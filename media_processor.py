import os
import re
import requests
import logging
from logging.handlers import RotatingFileHandler
import json
import shutil
import time
from dotenv import load_dotenv
from datetime import datetime, timezone
import threading
import subprocess
import pending_deletions
from episeerr import normalize_url
from episeerr_utils import validate_series_tag, sync_rule_tag_to_sonarr
from logging_config import main_logger as logger
# Load environment variables
load_dotenv()

# ============================================================
# DATABASE SUPPORT - Get settings from DB first, fallback to .env
# ============================================================
from settings_db import get_sonarr_config, get_service

def get_sonarr_settings():
    config = get_sonarr_config()
    if config:
        return normalize_url(config.get('url')), config.get('api_key')
    # Fallback to env
    return normalize_url(os.getenv('SONARR_URL')), os.getenv('SONARR_API_KEY')

def get_jellyfin_settings():
    config = get_service('jellyfin', 'default')
    if config:
        return (normalize_url(config.get('url')), 
                config.get('api_key'),
                config.get('config', {}).get('user_id'))
    # Fallback to env
    return (normalize_url(os.getenv('JELLYFIN_URL')),
            os.getenv('JELLYFIN_API_KEY'),
            os.getenv('JELLYFIN_USER_ID'))

def get_tautulli_settings():
    config = get_service('tautulli', 'default')
    if config:
        return normalize_url(config.get('url')), config.get('api_key')
    # Fallback to env
    return normalize_url(os.getenv('TAUTULLI_URL')), os.getenv('TAUTULLI_API_KEY')

def get_emby_settings():
    config = get_service('emby', 'default')
    if config:
        return (normalize_url(config.get('url')), 
                config.get('api_key'),
                config.get('config', {}).get('user_id'))
    # Fallback to env
    return (normalize_url(os.getenv('EMBY_URL')),
            os.getenv('EMBY_API_KEY'),
            os.getenv('EMBY_USER_ID'))


LAST_PROCESSED_JELLYFIN_EPISODES = {}
LAST_PROCESSED_LOCK = threading.Lock()

# Load service configurations with database-first approach
def load_jellyfin_config():
    """Load Jellyfin config from database or env with defaults"""
    from settings_db import get_jellyfin_config
    config = get_jellyfin_config()
    
    if config:
        return {
            'trigger_percentage': config.get('trigger_min', 50.0),  # Use trigger_min as primary percentage
            'poll_interval': config.get('poll_interval', 900),
            'trigger_min': config.get('trigger_min', 50.0),
            'trigger_max': config.get('trigger_max', 55.0)
        }
    
    # Fallback to env
    return {
        'trigger_percentage': float(os.getenv('JELLYFIN_TRIGGER_PERCENTAGE', '50.0')),
        'poll_interval': int(os.getenv('JELLYFIN_POLL_INTERVAL', '900')),
        'trigger_min': float(os.getenv('JELLYFIN_TRIGGER_MIN', '50.0')),
        'trigger_max': float(os.getenv('JELLYFIN_TRIGGER_MAX', '55.0'))
    }

def load_emby_config():
    """Load Emby config from database or env with defaults"""
    from settings_db import get_emby_config
    config = get_emby_config()
    
    if config:
        return {
            'trigger_percentage': config.get('trigger_percentage', 50.0),
            'poll_interval': config.get('poll_interval', 900)
        }
    
    # Fallback to env (with Jellyfin fallback for backward compatibility)
    return {
        'trigger_percentage': float(os.getenv('EMBY_TRIGGER_PERCENTAGE', os.getenv('JELLYFIN_TRIGGER_PERCENTAGE', '50.0'))),
        'poll_interval': int(os.getenv('EMBY_POLL_INTERVAL', os.getenv('JELLYFIN_POLL_INTERVAL', '900')))
    }

# Load configurations
_jellyfin_config = load_jellyfin_config()
_emby_config = load_emby_config()

# User-configurable settings for active polling
JELLYFIN_TRIGGER_PERCENTAGE = _jellyfin_config['trigger_percentage']
JELLYFIN_POLL_INTERVAL = _jellyfin_config['poll_interval']
JELLYFIN_TRIGGER_MIN = _jellyfin_config['trigger_min']
JELLYFIN_TRIGGER_MAX = _jellyfin_config['trigger_max']

# Emby configuration
EMBY_TRIGGER_PERCENTAGE = _emby_config['trigger_percentage']
EMBY_POLL_INTERVAL = _emby_config['poll_interval']

# Track processed episodes to prevent duplicates
processed_jellyfin_episodes = set()

# Define log paths
LOG_PATH = os.getenv('LOG_PATH', '/app/logs/app.log')
MISSING_LOG_PATH = os.getenv('MISSING_LOG_PATH', '/app/logs/missing.log')
CLEANUP_LOG_PATH = os.getenv('CLEANUP_LOG_PATH', '/app/logs/cleanup.log')

# Ensure log directories exist
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
os.makedirs(os.path.dirname(MISSING_LOG_PATH), exist_ok=True)
os.makedirs(os.path.dirname(CLEANUP_LOG_PATH), exist_ok=True)


# Global variables for session tracking
active_polling_sessions = {}
polling_threads = {}
polling_lock = threading.Lock()
# Global variables for active polling system
jellyfin_polling_thread = None
jellyfin_polling_running = False
processed_episodes = {}  # Track what we've already processed

# Configure root logger with minimal handlers (console only)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # Console logging only
    ]
)



# Create missing logger for missing series
missing_logger = logging.getLogger('missing')
missing_logger.setLevel(logging.INFO)
missing_logger.handlers.clear()  # Clear any inherited handlers
missing_logger.propagate = False  # Prevent propagation to root logger

# Add file handler for missing logger
missing_handler = logging.FileHandler(MISSING_LOG_PATH)  # /app/logs/missing.log
missing_handler.setLevel(logging.INFO)
missing_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
missing_logger.addHandler(missing_handler)

# Add console handler for missing logger
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
missing_logger.addHandler(console_handler)



# Enhanced logging setup for cleanup operations
def setup_cleanup_logging():
    """Setup cleanup logging to write to BOTH console AND files."""
    # Create cleanup-specific logger
    cleanup_logger = logging.getLogger('cleanup')
    cleanup_logger.setLevel(logging.INFO)
    cleanup_logger.handlers.clear()  # Clear any inherited handlers
    cleanup_logger.propagate = False  # Prevent propagation to root logger
    
    # File handler for cleanup-specific log
    cleanup_file_handler = RotatingFileHandler(
        CLEANUP_LOG_PATH,  # /app/logs/cleanup.log
        maxBytes=5*1024*1024,  # 5 MB
        backupCount=5,
        encoding='utf-8'
    )
    cleanup_file_handler.setLevel(logging.INFO)
    cleanup_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    cleanup_file_handler.setFormatter(cleanup_formatter)
    
    # File handler for main app log (with CLEANUP prefix)
    main_file_handler = RotatingFileHandler(
        LOG_PATH,  # /app/logs/app.log
        maxBytes=10*1024*1024,  # 10 MB
        backupCount=3,
        encoding='utf-8'
    )
    main_file_handler.setLevel(logging.INFO)
    main_formatter = logging.Formatter('%(asctime)s - CLEANUP - %(levelname)s - %(message)s')
    main_file_handler.setFormatter(main_formatter)
    
    # Console handler for Docker logs
    console_handler_cleanup = logging.StreamHandler()
    console_handler_cleanup.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(asctime)s - CLEANUP - %(levelname)s - %(message)s')
    console_handler_cleanup.setFormatter(console_formatter)
    
    # Add handlers to cleanup logger
    cleanup_logger.addHandler(main_file_handler)
    cleanup_logger.addHandler(cleanup_file_handler)
    cleanup_logger.addHandler(console_handler_cleanup)
    
    return cleanup_logger

# Initialize cleanup logger
cleanup_logger = setup_cleanup_logging()

# Define global variables based on environment settings - DB FIRST!
SONARR_URL, SONARR_API_KEY = get_sonarr_settings()

# Initialize activity_storage with Sonarr config
try:
    from activity_storage import init_sonarr_config
    init_sonarr_config(SONARR_URL, SONARR_API_KEY)
    logger.info("✅ Activity storage initialized with Sonarr config")
except Exception as e:
    logger.warning(f"Could not initialize activity storage: {e}")

# Load settings from a JSON configuration file
def load_config():
    """Load configuration from JSON file."""
    config_path = os.getenv('CONFIG_PATH', '/app/config/config.json')
    
    # REMOVED: Backup on every load (was causing spam)
    with open(config_path, 'r') as file:
        config = json.load(file)
    
    # Ensure required keys are present with default values
    if 'rules' not in config:
        config['rules'] = {}
    
    return config


def save_config(config):
    """Load configuration from JSON file."""
    config_path = os.getenv('CONFIG_PATH', '/app/config/config.json')
    
    # REMOVED: Backup on every load (was causing spam)
    with open(config_path, 'r') as file:
        config = json.load(file)
    
    # Ensure required keys are present with default values
    if 'rules' not in config:
        config['rules'] = {}
    
    return config


def save_config(config):
    """Save configuration to JSON file with automatic backup."""
    config_path = os.getenv('CONFIG_PATH', '/app/config/config.json')
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    
    # Backup BEFORE saving (only when actually writing)
    if os.path.exists(config_path):
        backup_path = config_path + '.bak'
        try:
            shutil.copy2(config_path, backup_path)
            logger.debug(f"Backed up config.json to {backup_path}")
        except Exception as e:
            logger.warning(f"Could not backup config.json: {e}")
    
    # Save the config
    with open(config_path, 'w') as file:
        json.dump(config, file, indent=4)

def move_series_in_config(series_id, from_rule, to_rule):
    """
    Move a series from one rule to another in config.json, preserving activity data.
    This is called when tag drift is detected (user changed tag manually in Sonarr).
    
    Args:
        series_id: Sonarr series ID
        from_rule: Current rule name in config (from tag, may have different case)
        to_rule: Target rule name (from Sonarr tag, may have different case)
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        config = load_config()
        
        # Case-insensitive lookup for from_rule
        actual_from_rule = None
        for rn in config['rules'].keys():
            if rn.lower() == from_rule.lower():
                actual_from_rule = rn
                break

        if not actual_from_rule:
            logger.error(f"Source rule '{from_rule}' not found in config")
            return False

        # Case-insensitive lookup for to_rule
        actual_to_rule = None
        for rn in config['rules'].keys():
            if rn.lower() == to_rule.lower():
                actual_to_rule = rn
                break
            
        if not actual_to_rule:
            logger.error(f"Target rule '{to_rule}' not found in config")
            return False
        
        # Get series data from source rule
        source_series = config['rules'][actual_from_rule].get('series', {})
        series_id_str = str(series_id)
        
        if series_id_str not in source_series:
            logger.warning(f"Series {series_id} not found in rule '{actual_from_rule}'")
            return False
        
        # Get the series data (preserve activity info)
        series_data = source_series[series_id_str]
        
        # Remove from source rule
        del source_series[series_id_str]
        logger.info(f"Removed series {series_id} from rule '{actual_from_rule}'")
        
        # Add to target rule
        target_series = config['rules'][actual_to_rule].setdefault('series', {})
        target_series[series_id_str] = series_data
        logger.info(f"Added series {series_id} to rule '{actual_to_rule}' (preserving activity data)")
        
        # Save config
        save_config(config)
        
        # Sync tag in Sonarr to ensure consistency (remove any duplicates)
        from episeerr_utils import sync_rule_tag_to_sonarr
        sync_rule_tag_to_sonarr(series_id, actual_to_rule)
        
        return True
        
    except Exception as e:
        logger.error(f"Error moving series {series_id}: {str(e)}")
        return False
    
def get_episode_details_by_id(episode_id):
    """Get episode details by episode ID from Sonarr."""
    try:
        url = f"{SONARR_URL}/api/v3/episode/{episode_id}"
        headers = {'X-Api-Key': SONARR_API_KEY}
        response = requests.get(url, headers=headers, timeout=5)
        
        if response.ok:
            return response.json()
        else:
            logger.error(f"Failed to get episode {episode_id}: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"Error getting episode {episode_id}: {str(e)}")
        return None
    
def update_activity_date(series_id, season_number=None, episode_number=None, timestamp=None):
    """
    Update activity date in config.json (PRIMARY SOURCE).
    This becomes the authoritative date that overrides external services.
    """
    try:
        config = load_config()
        current_time = timestamp or int(time.time())
        
        # Find the series in rules and update activity_date
        updated = False
        for rule_name, rule_details in config['rules'].items():
            series_dict = rule_details.get('series', {})
            if str(series_id) in series_dict:
                # Get grace_scope to determine tracking method
                grace_scope = rule_details.get('grace_scope', 'series')
                
                if grace_scope == 'season':
                    # PER-SEASON TRACKING
                    # Ensure series data structure exists
                    if not isinstance(series_dict[str(series_id)], dict):
                        series_dict[str(series_id)] = {}
                    
                    series_data = series_dict[str(series_id)]
                    
                    # Update overall series activity (for Dormant timer)
                    series_data['activity_date'] = current_time
                    
                    # Ensure seasons dict exists
                    if 'seasons' not in series_data:
                        series_data['seasons'] = {}
                    
                    # Update specific season activity (for Grace timers)
                    season_key = str(season_number)
                    if season_key not in series_data['seasons']:
                        series_data['seasons'][season_key] = {}
                    
                    series_data['seasons'][season_key]['activity_date'] = current_time
                    series_data['seasons'][season_key]['last_episode'] = episode_number
                    
                    logger.info(f"📺 Updated PER-SEASON activity for series {series_id} Season {season_number}: S{season_number}E{episode_number} at {datetime.fromtimestamp(current_time)}")
                else:
                    # PER-SERIES TRACKING (default/legacy behavior)
                    series_dict[str(series_id)] = {
                        'activity_date': current_time,
                        'last_season': season_number,
                        'last_episode': episode_number
                    }
                    logger.info(f"📺 Updated PER-SERIES activity for series {series_id}: S{season_number}E{episode_number} at {datetime.fromtimestamp(current_time)}")
                
                updated = True
                break
        
        if updated:
            save_config(config)
            logger.info(f"✅ Config saved - series {series_id} activity data updated")
            
            # NEW: Clear grace_cleaned flag - allows re-entry to grace cleanup
            try:
                config = load_config()  # Reload to ensure we have latest
                for rule_name, rule_details in config['rules'].items():
                    series_dict = rule_details.get('series', {})
                    if str(series_id) in series_dict:
                        series_data = series_dict[str(series_id)]
                        if isinstance(series_data, dict):
                            series_data['grace_cleaned'] = False
                            save_config(config)
                            logger.info(f"🔄 Watch detected - cleared grace_cleaned flag for series {series_id}")
                        break
            except Exception as e:
                logger.debug(f"Could not clear grace_cleaned flag: {e}")
            
            # NEW: Log watch event
            try:
                from activity_storage import save_watch_event
                
                # Get series title from Sonarr
                headers = {'X-Api-Key': SONARR_API_KEY}
                response = requests.get(f"{SONARR_URL}/api/v3/series/{series_id}", headers=headers, timeout=5)
                
                if response.ok:
                    series_info = response.json()
                    save_watch_event(
                        series_id=series_id,
                        series_title=series_info['title'],
                        season=season_number,
                        episode=episode_number,
                        user="System"
                    )
            except Exception as e:
                logger.debug(f"Could not log watch event: {e}")
        else:
            logger.warning(f"Series {series_id} not found in any rule for activity update")
        
    except Exception as e:
        logger.error(f"Error updating activity date for series {series_id}: {str(e)}")

def get_activity_date_with_hierarchy(series_id, series_title=None, return_complete=False):
    """
    Get activity date using hierarchy: config.json, Tautulli, Jellyfin, Sonarr.
    
    Args:
        return_complete: If True, returns (timestamp, season, episode) when available
                        If False, returns just timestamp (existing behavior)
    """
    logger.info(f"🔍 Getting activity date for series {series_id} ({series_title})")
    
    # Step 1: Check config.json (PRIMARY SOURCE)
    config = load_config()
    for rule_name, rule_details in config['rules'].items():
        series_dict = rule_details.get('series', {})
        series_data = series_dict.get(str(series_id))
        if isinstance(series_data, dict):
            activity_date = series_data.get('activity_date')
            if activity_date:
                if return_complete:
                    last_season = series_data.get('last_season')
                    last_episode = series_data.get('last_episode')
                    if last_season and last_episode:
                        logger.info(f"✅ Using complete config data for series {series_id}: S{last_season}E{last_episode} at {datetime.fromtimestamp(activity_date)}")
                        return activity_date, last_season, last_episode
                    else:
                        logger.info(f"⚠️ Config has activity_date but missing season/episode data")
                        # Continue to external sources for complete data
                else:
                    logger.info(f"✅ Using config activity date for series {series_id}: {datetime.fromtimestamp(activity_date)}")
                    return activity_date
    
    logger.info(f"⚠️  No config activity date for series {series_id}")
    
    # Get Sonarr title if not provided
    if not series_title:
        try:
            headers = {'X-Api-Key': SONARR_API_KEY}
            response = requests.get(f"{SONARR_URL}/api/v3/series/{series_id}", headers=headers, timeout=5)
            if response.ok:
                series_title = response.json().get('title')
                logger.info(f"Retrieved Sonarr title: {series_title}")
        except Exception as e:
            logger.warning(f"Failed to get Sonarr title for series {series_id}: {str(e)}")
    
    # Step 2: Check external services (only if title available)
    if series_title:
        # Check which external service is configured (user typically has one, not both)
        tautulli_url, tautulli_api_key = get_tautulli_settings()
        jellyfin_url, jellyfin_api_key, _ = get_jellyfin_settings()
        
        # Prefer Tautulli if both are configured (since it's more accurate for watch tracking)
        if tautulli_url and tautulli_api_key:
            logger.info(f"🔍 Checking Tautulli for '{series_title}'")
            
            # Use enhanced Tautulli function
            tautulli_result = get_tautulli_last_watched(series_title, return_complete=return_complete)
            if tautulli_result:
                if return_complete and isinstance(tautulli_result, tuple):
                    timestamp, season, episode = tautulli_result
                    logger.info(f"✅ Using complete Tautulli data for series {series_id}: S{season}E{episode} at {datetime.fromtimestamp(timestamp)}")
                    return timestamp, season, episode
                elif not return_complete:
                    logger.info(f"✅ Using Tautulli date for series {series_id}: {datetime.fromtimestamp(tautulli_result)}")
                    return tautulli_result
            logger.info(f"⚠️  No Tautulli date found for series {series_id}")
            
        elif jellyfin_url and jellyfin_api_key:
            logger.info(f"🔍 Checking Jellyfin for '{series_title}'")
            
            # Use enhanced Jellyfin function
            jellyfin_result = get_jellyfin_last_watched(series_title, return_complete=return_complete)
            if jellyfin_result:
                if return_complete and isinstance(jellyfin_result, tuple):
                    timestamp, season, episode = jellyfin_result
                    logger.info(f"✅ Using complete Jellyfin data for series {series_id}: S{season}E{episode} at {datetime.fromtimestamp(timestamp)}")
                    return timestamp, season, episode
                elif not return_complete:
                    logger.info(f"✅ Using Jellyfin date for series {series_id}: {datetime.fromtimestamp(jellyfin_result)}")
                    return jellyfin_result
            logger.info(f"⚠️  No Jellyfin date found for series {series_id}")
            
        else:
            logger.info(f"⚠️  No external watch tracking configured (Tautulli/Jellyfin)")
    
    # Step 3: Check Sonarr episode file dates (FINAL FALLBACK)
    logger.info(f"🔍 Checking Sonarr file dates for series {series_id}")
    sonarr_date = get_sonarr_latest_file_date(series_id)
    if sonarr_date:
        if return_complete:
            logger.info(f"✅ Using Sonarr file date with S1E1 fallback for series {series_id}: {datetime.fromtimestamp(sonarr_date)}")
            return sonarr_date, 1, 1
        else:
            logger.info(f"✅ Using Sonarr file date for series {series_id}: {datetime.fromtimestamp(sonarr_date)}")
            return sonarr_date
    
    logger.warning(f"⚠️  No activity date found for series {series_id}")
    if return_complete:
        return None, None, None
    return None
def is_in_trigger_window(progress):
    """Check if progress is within trigger window"""
    return JELLYFIN_TRIGGER_MIN <= progress <= JELLYFIN_TRIGGER_MAX

def get_episode_tracking_key(series_name, season, episode, user_name):
    """Generate unique key for tracking processed episodes"""
    return f"{series_name}:S{season}E{episode}:{user_name}"

def check_jellyfin_user(username):
    """Check if this is the configured Jellyfin user"""
    _, _, configured_user = get_jellyfin_settings()
    if not configured_user:
        logger.warning("JELLYFIN_USER_ID not set - processing all users")
        return True
    return username.lower() == configured_user.lower()

def check_emby_user(username):
    """Check if this is the configured Emby user.
    Reads EMBY_USER_ID; falls back to JELLYFIN_USER_ID if not set."""
    _, _, configured_user = get_emby_settings()
    if not configured_user:
        logger.warning("EMBY_USER_ID not set - processing all users")
        return True
    return username.lower() == configured_user.lower()

def get_server_activity():
    """Read current viewing details from server webhook stored data."""
    try:
        filepath = '/app/temp/data_from_server.json'
        if not os.path.exists(filepath):
            filepath = '/app/temp/data_from_tautulli.json'
            
        with open(filepath, 'r') as file:
            data = json.load(file)
        
        # Try server-prefix fields first (new format)
        series_title = data.get('server_title')
        season_number = data.get('server_season_num')
        episode_number = data.get('server_ep_num')
        thetvdb_id = data.get('thetvdb_id')
        themoviedb_id = data.get('themoviedb_id')
        
        # If not found, try plex-prefix fields (backward compatibility)
        if not all([series_title, season_number, episode_number]):
            series_title = data.get('plex_title')
            season_number = data.get('plex_season_num')
            episode_number = data.get('plex_ep_num')
        
        if all([series_title, season_number, episode_number]):
            return series_title, int(season_number), int(episode_number), thetvdb_id, themoviedb_id
            
        logger.error(f"Required data fields not found in {filepath}")
        return None, None, None, None, None
        
    except Exception as e:
        logger.error(f"Failed to read or parse data from server webhook: {str(e)}")
    
    return None, None, None, None, None

def better_partial_match(webhook_title, sonarr_title):
    webhook_clean = webhook_title.lower().strip()
    sonarr_clean = sonarr_title.lower().strip()
    
    # Original bidirectional matching
    if webhook_clean in sonarr_clean or sonarr_clean in webhook_clean:
        return True
    
    # Check if they start with the same base title (for international variants)
    webhook_base = webhook_clean.split(':')[0].strip()
    sonarr_base = sonarr_clean.split(':')[0].strip()
    
    return webhook_base == sonarr_base and len(webhook_base) > 3

def get_series_id(series_name, thetvdb_id=None, themoviedb_id=None):
    """Fetch series ID by name from Sonarr with improved matching."""
    url = f"{SONARR_URL}/api/v3/series"
    headers = {'X-Api-Key': SONARR_API_KEY}
    try:
        response = requests.get(url, headers=headers)
        if not response.ok:
            logger.error(f"Failed to fetch series from Sonarr: {response.status_code}")
            return None
        
        series_list = response.json()
        
        # 1. TVDB ID matching (most reliable)
        if thetvdb_id:
            try:
                tvdb_id_int = int(thetvdb_id)
                for series in series_list:
                    if series.get('tvdbId') == tvdb_id_int:
                        logger.info(f"Found TVDB ID match: {series['title']} (TVDB: {thetvdb_id})")
                        return series['id']
            except (ValueError, TypeError):
                pass
        
        # 2. TMDB ID matching
        if themoviedb_id:
            try:
                tmdb_id_int = int(themoviedb_id)
                for series in series_list:
                    if series.get('tmdbId') == tmdb_id_int:
                        logger.info(f"Found TMDB ID match: {series['title']} (TMDB: {themoviedb_id})")
                        return series['id']
            except (ValueError, TypeError):
                pass
        
        # 3. Exact title match
        for series in series_list:
            if series['title'].lower() == series_name.lower():
                logger.info(f"Found exact match: {series['title']}")
                return series['id']
        
        # 4. Match without year suffixes
        webhook_title_clean = re.sub(r'\s*\(\d{4}\)$', '', series_name).strip()
        for series in series_list:
            sonarr_title_clean = re.sub(r'\s*\(\d{4}\)$', '', series['title']).strip()
            if sonarr_title_clean.lower() == webhook_title_clean.lower():
                logger.info(f"Found match ignoring year: '{series['title']}' matches '{series_name}'")
                return series['id']
        
        
        
        # 5. Log close matches for debugging
        close_matches = []
        for series in series_list:
            if series_name.lower() in series['title'].lower():
                close_matches.append(series['title'])
        
        if close_matches:
            missing_logger.info(f"Series not found in Sonarr: '{series_name}'. Possible matches: {close_matches}")
        else:
            missing_logger.info(f"Series not found in Sonarr: '{series_name}'. No close matches.")
        return None
        
    except Exception as e:
        logger.error(f"Error in series lookup: {str(e)}")
        return None

def get_episode_details(series_id, season_number):
    """Fetch details of episodes for a specific series and season from Sonarr."""
    url = f"{SONARR_URL}/api/v3/episode?seriesId={series_id}&seasonNumber={season_number}"
    headers = {'X-Api-Key': SONARR_API_KEY}
    response = requests.get(url, headers=headers)
    if response.ok:
        return response.json()
    logger.error("Failed to fetch episode details.")
    return []

def monitor_or_search_episodes(episode_ids, action_option, series_id=None, series_title=None, get_type='episodes'):
    """Either monitor or trigger a search for episodes in Sonarr based on the action_option."""
    if not episode_ids:
        logger.info("No episodes to monitor/search")
        return
        
    monitor_episodes(episode_ids, True)
    if action_option == "search":
        trigger_episode_search_in_sonarr(episode_ids, series_id, series_title, get_type)


def monitor_episodes(episode_ids, monitor=True):
    """Set episodes to monitored or unmonitored in Sonarr."""
    if not episode_ids:
        return
        
    url = f"{SONARR_URL}/api/v3/episode/monitor"
    headers = {'X-Api-Key': SONARR_API_KEY, 'Content-Type': 'application/json'}
    data = {"episodeIds": episode_ids, "monitored": monitor}
    response = requests.put(url, json=data, headers=headers)
    if response.ok:
        action = "monitored" if monitor else "unmonitored"
        logger.info(f"Episodes {episode_ids} successfully {action}.")
    else:
        logger.error(f"Failed to set episodes {action}. Response: {response.text}")


def trigger_episode_search_in_sonarr(episode_ids, series_id=None, series_title=None, get_type='episodes'):
    """
    Trigger a search for specified episodes in Sonarr and send pending notification
    
    Args:
        episode_ids: List of Sonarr episode IDs to search
        series_id: Sonarr series ID
        series_title: Series name for notifications
        get_type: Rule type ('seasons' or 'episodes') - determines season pack preference
    """
    if not episode_ids:
        return
        
    url = f"{SONARR_URL}/api/v3/command"
    headers = {'X-Api-Key': SONARR_API_KEY, 'Content-Type': 'application/json'}
    
    # NEW: If rule type is 'seasons', prefer season packs
    if get_type == 'seasons':
        # Get the season number from the first episode
        episode = get_episode_details_by_id(episode_ids[0])
        if episode and series_id:
            first_season = episode['seasonNumber']
            
            # BUGFIX: If there are multiple episodes, check if we should search the NEXT season
            # This happens when the first episode is the remainder of the current season
            if len(episode_ids) > 1:
                # Check the second episode's season
                second_episode = get_episode_details_by_id(episode_ids[1])
                if second_episode and second_episode['seasonNumber'] > first_season:
                    # The bulk of episodes are in the next season, search for that
                    season_to_search = second_episode['seasonNumber']
                    logger.info(f"Rule type is 'seasons' - searching for season pack for Season {season_to_search} (next full season)")
                else:
                    # All episodes in same season
                    season_to_search = first_season
                    logger.info(f"Rule type is 'seasons' - searching for season pack for Season {season_to_search}")
            else:
                # Only one episode, use its season
                season_to_search = first_season
                logger.info(f"Rule type is 'seasons' - searching for season pack for Season {season_to_search}")
            
            # Use SeasonSearch to prefer season packs
            data = {
                "name": "SeasonSearch",
                "seriesId": series_id,
                "seasonNumber": season_to_search
            }
        else:
            # Fallback to episode search if we can't determine season
            logger.warning("Could not determine season, falling back to episode search")
            data = {"name": "EpisodeSearch", "episodeIds": episode_ids}
    else:
        # Default: Individual episode search
        data = {"name": "EpisodeSearch", "episodeIds": episode_ids}
    
    response = requests.post(url, json=data, headers=headers)
    
    if response.ok:
        search_type = "Season pack search" if get_type == 'seasons' else "Episode search"
        logger.info(f"{search_type} command sent to Sonarr successfully.")

        # Log search event
        if series_id and series_title and episode_ids:
            from activity_storage import save_search_event
            # Get season/episode from first episode ID
            episode = get_episode_details_by_id(episode_ids[0])
            if episode:
                save_search_event(
                    series_id=series_id,
                    series_title=series_title,
                    season=episode['seasonNumber'],
                    episode=episode['episodeNumber'],
                    episode_ids=episode_ids
                )
    else:
        logger.error(f"Failed to send search command. Response: {response.text}")
        return
    
    # Send pending notification and store message ID
    if series_id and series_title:
        try:
            from notifications import send_notification
            
            episode_details = get_episode_details_by_id(episode_ids[0])
            if episode_details:
                season_number = episode_details['seasonNumber']
                episode_number = episode_details['episodeNumber']
                
                # Send notification using the notifications module
                message_id = send_notification(
                    "episode_search_pending",
                    series=series_title,
                    season=season_number,
                    episode=episode_number,
                    air_date=episode_details.get('airDateUtc'),
                    series_id=series_id
                )
                
                if message_id:
                    store_pending_search(series_id, episode_ids, message_id)
                    logger.info(f"Stored pending search notification: {message_id}")
        except Exception as e:
            logger.debug(f"Could not send pending notification: {str(e)}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")

def unmonitor_episodes(episode_ids):
    """Unmonitor specified episodes in Sonarr."""
    if episode_ids:
        monitor_episodes(episode_ids, False)

def fetch_next_episodes_dropdown(series_id, season_number, episode_number, get_type, get_count):
    """
    Fetch next episodes using dropdown system (get_type + get_count).
    Assumes linear watching only.
    """
    next_episode_ids = []

    try:
        if get_type == "all":
            # Get all episodes from current position forward
            all_episodes = fetch_all_episodes(series_id)
            sorted_episodes = sorted(all_episodes, key=lambda ep: (ep['seasonNumber'], ep['episodeNumber']))
            
            # Find current position and get everything after
            for ep in sorted_episodes:
                if (ep['seasonNumber'] > season_number or 
                    (ep['seasonNumber'] == season_number and ep['episodeNumber'] > episode_number)):
                    next_episode_ids.append(ep['id'])
            return next_episode_ids
            
        elif get_type == 'seasons':
            # Get X full seasons starting from remaining current season
            current_season_episodes = get_episode_details(series_id, season_number)
            remaining_current = [ep['id'] for ep in current_season_episodes if ep['episodeNumber'] > episode_number]
            next_episode_ids.extend(remaining_current)
            
            # Get additional full seasons if needed
            seasons_to_get = get_count if get_count else 1
            if not remaining_current:
                # Current season finished, get next X seasons
                for season_offset in range(1, seasons_to_get + 1):
                    season_episodes = get_episode_details(series_id, season_number + season_offset)
                    next_episode_ids.extend([ep['id'] for ep in season_episodes])
            elif seasons_to_get > 1:
                # Get additional seasons beyond current
                for season_offset in range(1, seasons_to_get):
                    season_episodes = get_episode_details(series_id, season_number + season_offset)
                    next_episode_ids.extend([ep['id'] for ep in season_episodes])
                    
            logger.info(f"Dropdown seasons mode: Found {len(next_episode_ids)} episodes across {seasons_to_get} seasons")
            return next_episode_ids
            
        else:  # episodes
            # Get specific number of episodes in linear order
            num_episodes = get_count if get_count else 1
            
            # Get remaining episodes in current season first
            current_season_episodes = get_episode_details(series_id, season_number)
            remaining_episodes = [ep['id'] for ep in current_season_episodes if ep['episodeNumber'] > episode_number]
            next_episode_ids.extend(remaining_episodes)

            # If we need more episodes, get from subsequent seasons
            current_season_num = season_number + 1
            while len(next_episode_ids) < num_episodes:
                next_season_episodes = get_episode_details(series_id, current_season_num)
                if not next_season_episodes:
                    logger.info(f"No more episodes available after season {current_season_num - 1}")
                    break
                    
                remaining_needed = num_episodes - len(next_episode_ids)
                next_episode_ids.extend([ep['id'] for ep in next_season_episodes[:remaining_needed]])
                current_season_num += 1
                
                # Prevent infinite loops
                if current_season_num > season_number + 10:
                    logger.warning(f"Stopping after checking 10 seasons ahead")
                    break

            logger.info(f"Dropdown episodes mode: Found {len(next_episode_ids)} out of {num_episodes} requested")
            return next_episode_ids[:num_episodes]
            
    except Exception as e:
        logger.error(f"Error in dropdown fetch_next_episodes: {str(e)}")
        return []

def fetch_all_episodes(series_id):
    """Fetch all episodes for a series from Sonarr."""
    url = f"{SONARR_URL}/api/v3/episode?seriesId={series_id}"
    headers = {'X-Api-Key': SONARR_API_KEY}
    response = requests.get(url, headers=headers)
    if response.ok:
        return response.json()
    logger.error("Failed to fetch all episodes.")
    return []

def get_tautulli_last_watched(series_title, return_complete=False):
    """
    Get last watched date from Tautulli - ENHANCED VERSION.
    
    Args:
        return_complete: If True, returns (timestamp, season, episode)
                        If False, returns just timestamp (existing behavior)
    """
    try:
        tautulli_url, tautulli_api_key = get_tautulli_settings()
        
        if not tautulli_url or not tautulli_api_key:
            logger.warning(f"Tautulli not configured")
            return None
        
        def normalize_title(title):
            title = title.lower()
            title = re.sub(r'\s*\(\d{4}\)', '', title)  # Remove year
            title = re.sub(r'[^\w\s]', ' ', title)      # Remove special chars
            return ' '.join(title.split())
        
        normalized_series_title = normalize_title(series_title)
        
        # Create smart title variations
        title_variations = [
            series_title,                                    # Original
            re.sub(r'\s*\(\d{4}\)', '', series_title),      # No year
            series_title.replace(": ", " - "),              # Colon variants
            series_title.replace(": ", " "),
            series_title.split(" (")[0],                     # Before parentheses
        ]
        
        # Try each variation (but limit API calls)
        for search_title in set(title_variations[:3]):  # Limit to top 3 variations
            normalized_search = normalize_title(search_title)
            logger.debug(f"Trying Tautulli title: '{search_title}'")
            
            params = {
                'apikey': tautulli_api_key,
                'cmd': 'get_history',
                'media_type': 'episode',
                'search': search_title,
                'length': 1
            }
            
            response = requests.get(f"{tautulli_url}/api/v2", params=params, timeout=10)
            
            if not response.ok:
                logger.warning(f"Tautulli API error: {response.status_code}")
                continue
                
            data = response.json()
            
            if data.get('response', {}).get('result') != 'success':
                continue
            
            history = data.get('response', {}).get('data', {}).get('data', [])
            
            if not history:
                continue
                
            most_recent = history[0]
            entry_title = most_recent.get('grandparent_title', '')
            normalized_entry = normalize_title(entry_title)
            
            # Check if titles match
            if (normalized_entry == normalized_search or 
                normalized_entry in normalized_series_title or 
                normalized_series_title in normalized_entry):
                
                last_watched = most_recent.get('date')
                
                if last_watched:
                    try:
                        timestamp = int(last_watched)
                        
                        if return_complete:
                            # Extract season and episode data
                            season_num = most_recent.get('parent_media_index')  # Season number
                            episode_num = most_recent.get('media_index')        # Episode number
                            
                            if season_num and episode_num:
                                season = int(season_num)
                                episode = int(episode_num)
                                logger.info(f"Found complete Tautulli data for '{entry_title}': S{season}E{episode} at {datetime.fromtimestamp(timestamp)}")
                                return timestamp, season, episode
                            else:
                                # Fallback to timestamp with default season/episode
                                logger.info(f"Found Tautulli timestamp for '{entry_title}' with S1E1 fallback: {datetime.fromtimestamp(timestamp)}")
                                return timestamp, 1, 1
                        else:
                            # Existing behavior - just timestamp
                            logger.info(f"Found Tautulli watch for '{entry_title}': {datetime.fromtimestamp(timestamp)}")
                            return timestamp
                            
                    except (ValueError, TypeError):
                        continue
        
        logger.info(f"No Tautulli watch history found for '{series_title}'")
        return None
        
    except requests.exceptions.Timeout:
        logger.error(f"Tautulli timeout for series '{series_title}'")
        return None
    except Exception as e:
        logger.error(f"Tautulli error for series '{series_title}': {str(e)}")
        return None

def get_jellyfin_user_id(jellyfin_url, jellyfin_api_key, username):
    """Get Jellyfin User ID (GUID) from username."""
    try:
        headers = {'X-Emby-Token': jellyfin_api_key}
        response = requests.get(f"{jellyfin_url}/Users", headers=headers, timeout=10)
        
        if response.ok:
            users = response.json()
            for user in users:
                if user.get('Name', '').lower() == username.lower():
                    user_id = user.get('Id')
                    logger.info(f"Found Jellyfin User ID for '{username}': {user_id}")
                    return user_id
            
            logger.warning(f"Username '{username}' not found in Jellyfin users")
            # Log available usernames for debugging
            available_users = [user.get('Name', 'Unknown') for user in users]
            logger.debug(f"Available Jellyfin users: {available_users}")
        else:
            logger.warning(f"Failed to get Jellyfin users: {response.status_code}")
        
        return None
        
    except Exception as e:
        logger.error(f"Error getting Jellyfin User ID: {str(e)}")
        return None
    
def process_jellyfin_progress_webhook(session_id, series_name, season_number, episode_number, progress_percent, user_name):
    """Process Jellyfin progress webhook with window-based deduplication."""
    
    # Check if in trigger window
    if not is_in_trigger_window(progress_percent):
        return False
    
    # Create tracking key
    tracking_key = get_episode_tracking_key(series_name, season_number, episode_number, user_name)
    
    # Check if already processed (silently skip)
    if tracking_key in processed_jellyfin_episodes:
        return False  # Don't log - already processed
    
    # Mark as processed FIRST
    processed_jellyfin_episodes.add(tracking_key)
    
    # NOW log (only happens once)
    logger.info(f"🎯 Processing Jellyfin episode at {progress_percent:.1f}% (window: {JELLYFIN_TRIGGER_MIN}-{JELLYFIN_TRIGGER_MAX}%)")
    logger.info(f"   📺 {series_name} S{season_number}E{episode_number} (User: {user_name})")
    
    try:
        # Call your existing processing logic
        episode_info = {
            'user_name': user_name,
            'series_name': series_name,
            'season_number': season_number,
            'episode_number': episode_number,
            'progress_percent': progress_percent
        }
        
        success = process_jellyfin_episode(episode_info)
        
        if not success:
            logger.warning(f"Processing failed for {tracking_key}")
        
        return success
        
    except Exception as e:
        logger.error(f"Error processing Jellyfin episode: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False

def cleanup_jellyfin_tracking():
    """Clear all tracking when playback stops"""
    count = len(processed_jellyfin_episodes)
    processed_jellyfin_episodes.clear()
    logger.debug(f"Cleared {count} Jellyfin tracking entries")

def get_jellyfin_last_watched(series_title, return_complete=False):
    """
    Get last watched date from Jellyfin - ENHANCED VERSION.
    
    Args:
        return_complete: If True, returns (timestamp, season, episode) when available
                        If False, returns just timestamp (existing behavior)
    """
    try:
        jellyfin_url, jellyfin_api_key, jellyfin_user_input = get_jellyfin_settings()
        
        if not all([jellyfin_url, jellyfin_api_key, jellyfin_user_input]):
            logger.warning("Jellyfin not configured")
            return None
        
        # Check if the user input is already a GUID (contains hyphens) or a username
        if '-' in jellyfin_user_input and len(jellyfin_user_input) > 30:
            # Looks like a GUID already
            jellyfin_user_id = jellyfin_user_input
            logger.debug(f"Using provided GUID: {jellyfin_user_id}")
        else:
            # Looks like a username, convert to GUID
            logger.debug(f"Converting username '{jellyfin_user_input}' to User ID")
            jellyfin_user_id = get_jellyfin_user_id(jellyfin_url, jellyfin_api_key, jellyfin_user_input)
            
            if not jellyfin_user_id:
                logger.warning(f"Could not find User ID for username '{jellyfin_user_input}'")
                return None
        
        def normalize_title(title):
            title = title.lower()
            title = re.sub(r'\s*\(\d{4}\)', '', title)  # Remove year
            title = re.sub(r'[^\w\s]', ' ', title)      # Remove special chars
            return ' '.join(title.split())
        
        headers = {'X-Emby-Token': jellyfin_api_key}
        
        if return_complete:
            # Get detailed playback history to find last watched episode
            logger.debug(f"Getting complete Jellyfin data for '{series_title}'")
            
            # Get user's viewing history
            params = {
                'IncludeItemTypes': 'Episode',
                'Recursive': 'true',
                'Fields': 'UserData,ParentId,SeasonNumber,IndexNumber',
                'SortBy': 'DatePlayed',
                'SortOrder': 'Descending',
                'Limit': 50  # Get recent episodes
            }
            
            response = requests.get(f"{jellyfin_url}/Users/{jellyfin_user_id}/Items", 
                                  headers=headers, params=params, timeout=10)
            
            if response.ok:
                data = response.json()
                items = data.get('Items', [])
                
                normalized_series_title = normalize_title(series_title)
                
                # Find the most recent episode from this series
                for item in items:
                    series_name = item.get('SeriesName', '')
                    normalized_item = normalize_title(series_name)
                    
                    # Check for title match
                    if (normalized_series_title == normalized_item or
                        normalized_series_title in normalized_item or 
                        normalized_item in normalized_series_title):
                        
                        user_data = item.get('UserData', {})
                        last_played = user_data.get('LastPlayedDate')
                        season_number = item.get('ParentIndexNumber')  # Season
                        episode_number = item.get('IndexNumber')       # Episode
                        
                        if last_played and season_number and episode_number:
                            try:
                                # Handle Jellyfin's ISO date format
                                if last_played.endswith('Z'):
                                    dt = datetime.fromisoformat(last_played.replace('Z', '+00:00'))
                                else:
                                    dt = datetime.fromisoformat(last_played)
                                
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=timezone.utc)
                                    
                                timestamp = int(dt.timestamp())
                                season = int(season_number)
                                episode = int(episode_number)
                                
                                logger.info(f"Found complete Jellyfin data for '{series_name}': S{season}E{episode} at {dt}")
                                return timestamp, season, episode
                                
                            except (ValueError, TypeError) as e:
                                logger.warning(f"Invalid Jellyfin date format: {last_played} - {e}")
                                continue
        
        # Fallback to existing logic (series-level LastPlayedDate)
        params = {
            'IncludeItemTypes': 'Series',
            'Recursive': 'true',
            'Fields': 'UserData'
        }
        
        response = requests.get(f"{jellyfin_url}/Users/{jellyfin_user_id}/Items", 
                              headers=headers, params=params, timeout=10)
        
        if not response.ok:
            logger.warning(f"Jellyfin API error: {response.status_code} - {response.text[:200]}")
            return None
            
        data = response.json()
        items = data.get('Items', [])
        logger.debug(f"Jellyfin found {len(items)} series for user {jellyfin_user_input}")
        
        normalized_series_title = normalize_title(series_title)
        
        for item in items:
            item_name = item.get('Name', '')
            normalized_item = normalize_title(item_name)
            
            # Check for title match with flexible matching
            if (normalized_series_title == normalized_item or
                normalized_series_title in normalized_item or 
                normalized_item in normalized_series_title):
                
                logger.debug(f"Matched Jellyfin series: '{item_name}'")
                
                user_data = item.get('UserData', {})
                last_played = user_data.get('LastPlayedDate')
                
                if last_played:
                    try:
                        # Handle Jellyfin's ISO date format
                        if last_played.endswith('Z'):
                            dt = datetime.fromisoformat(last_played.replace('Z', '+00:00'))
                        else:
                            dt = datetime.fromisoformat(last_played)
                        
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                            
                        timestamp = int(dt.timestamp())
                        
                        if return_complete:
                            logger.info(f"Found Jellyfin series LastPlayedDate for '{item_name}' with S1E1 fallback: {dt}")
                            return timestamp, 1, 1  # Default fallback for series-level date
                        else:
                            logger.info(f"Found Jellyfin LastPlayedDate for '{item_name}': {dt}")
                            return timestamp
                        
                    except ValueError as e:
                        logger.warning(f"Invalid Jellyfin LastPlayedDate format: {last_played} - {e}")
                        continue
        
        logger.info(f"No Jellyfin watch history found for '{series_title}'")
        return None
        
    except requests.exceptions.Timeout:
        logger.error(f"Jellyfin timeout for series '{series_title}'")
        return None
    except Exception as e:
        logger.error(f"Jellyfin error for series '{series_title}': {str(e)}")
        return None

def get_sonarr_latest_file_date(series_id):
    """Get the most recent episode file date from Sonarr - FIXED VERSION."""
    try:
        headers = {'X-Api': SONARR_API_KEY}
        logger.info(f"Getting episode file dates for series {series_id}")
        
        # Use the correct endpoint for episode files
        response = requests.get(f"{SONARR_URL}/api/v3/episodefile?seriesId={series_id}", headers=headers, timeout=10)
        
        if not response.ok:
            logger.error(f"Failed to get episode files for series {series_id}: {response.status_code}")
            return None
        
        episode_files = response.json()
        logger.debug(f"Sonarr found {len(episode_files)} episode files")
        
        if not episode_files:
            logger.warning(f"No episode files found for series {series_id}")
            return None
        
        latest_file_date = None
        latest_episode_info = None
        
        for file_data in episode_files:
            season = file_data.get('seasonNumber')
            
            # Get episode numbers from the episodes array
            episodes = file_data.get('episodes', [])
            if episodes:
                episode_numbers = [ep.get('episodeNumber') for ep in episodes]
                ep_display = f"E{min(episode_numbers)}" if episode_numbers else "E?"
            else:
                ep_display = "E?"
            
            date_added_str = file_data.get('dateAdded')
            logger.debug(f"S{season}{ep_display}: dateAdded = '{date_added_str}'")
            
            if not date_added_str:
                logger.warning(f"Missing dateAdded for S{season}{ep_display}")
                continue
            
            timestamp = parse_date_fixed(date_added_str, f"S{season}{ep_display}")
            
            if timestamp:
                if not latest_file_date or timestamp > latest_file_date:
                    latest_file_date = timestamp
                    latest_episode_info = f"S{season}{ep_display}"
            else:
                logger.error(f"Failed to parse dateAdded for S{season}{ep_display}: '{date_added_str}'")
        
        if latest_file_date:
            logger.info(f"Latest file: {latest_episode_info} at {datetime.fromtimestamp(latest_file_date, tz=timezone.utc)} UTC")
            return latest_file_date
        else:
            logger.warning(f"No valid episode file dates found for series {series_id}")
            return None
            
    except requests.exceptions.Timeout:
        logger.error(f"Sonarr timeout for series {series_id}")
        return None
    except Exception as e:
        logger.error(f"Sonarr error for series {series_id}: {str(e)}")
        return None

def parse_date_fixed(date_str, context):
    """Parse date string with multiple formats - FIXED VERSION."""
    try:
        # Method 1: Handle Z suffix (UTC)
        if date_str.endswith('Z'):
            dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            timestamp = int(dt.timestamp())
            logger.debug(f"Parsed {context} ISO+Z: {timestamp} ({dt})")
            return timestamp
        
        # Method 2: Try direct ISO parsing
        try:
            dt = datetime.fromisoformat(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            timestamp = int(dt.timestamp())
            logger.debug(f"Parsed {context} ISO: {timestamp} ({dt})")
            return timestamp
        except ValueError:
            pass
        
        # Method 3: Strip milliseconds and try again
        if '.' in date_str:
            clean_date = re.sub(r'\\.\\d+', '', date_str)
            if clean_date.endswith('Z'):
                clean_date = clean_date.replace('Z', '+00:00')
            try:
                dt = datetime.fromisoformat(clean_date)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                timestamp = int(dt.timestamp())
                logger.debug(f"Parsed {context} no-ms: {timestamp} ({dt})")
                return timestamp
            except ValueError:
                pass
        
        logger.error(f"Could not parse date for {context}: '{date_str}'")
        return None
        
    except Exception as e:
        logger.error(f"Date parse error for {context}: {str(e)}")
        return None

def rule_to_legacy_params(rule):
    """Convert rule to legacy parameters for existing functions."""
    get_type = rule.get('get_type', 'episodes')
    get_count = rule.get('get_count', 1)
    keep_type = rule.get('keep_type', 'episodes') 
    keep_count = rule.get('keep_count', 1)
    
    # Convert get params
    if get_type == 'all':
        get_option = 'all'
    elif get_type == 'seasons':
        get_option = 'season' if get_count == 1 else str(get_count)
    else:
        get_option = str(get_count) if get_count else '1'
    
    # Convert keep params
    if keep_type == 'all':
        keep_watched = 'all'
    elif keep_type == 'seasons':
        keep_watched = 'season' if keep_count == 1 else str(keep_count)
    else:
        keep_watched = str(keep_count) if keep_count else '1'
    
    return get_option, keep_watched

def parse_legacy_value(value):
    """Parse legacy string value to new format."""
    if value == 'all':
        return 'all', None
    elif value == 'season':
        return 'seasons', 1
    else:
        try:
            count = int(value)
            return 'episodes', count
        except (ValueError, TypeError):
            return 'episodes', 1

def find_episodes_leaving_keep_block(all_episodes, keep_type, keep_count, last_watched_season, last_watched_episode):
    """
    Find episodes that are leaving the keep block using dropdown system.
    These episodes should be deleted immediately (real-time cleanup).
    """
    episodes_leaving = []
    
    try:
        if keep_type == "all":
            # Keep everything, nothing leaves
            return []
            
        elif keep_type == "seasons":
            # Keep X seasons, episodes from older seasons leave
            seasons_to_keep = keep_count if keep_count else 1
            cutoff_season = last_watched_season - seasons_to_keep + 1
            
            episodes_leaving = [
                ep for ep in all_episodes 
                if ep['seasonNumber'] < cutoff_season and ep.get('hasFile')
            ]
            
        else:  # episodes
            # Keep X episodes, older episodes leave the keep block
            episodes_to_keep = keep_count if keep_count else 1
            
            # Sort episodes by season/episode number
            sorted_episodes = sorted(all_episodes, key=lambda ep: (ep['seasonNumber'], ep['episodeNumber']))
            
            # Find the last watched episode index
            last_watched_index = None
            for i, ep in enumerate(sorted_episodes):
                if (ep['seasonNumber'] == last_watched_season and 
                    ep['episodeNumber'] == last_watched_episode):
                    last_watched_index = i
                    break
            
            if last_watched_index is not None:
                # Keep block: episodes_to_keep episodes ending with the one just watched
                keep_start_index = max(0, last_watched_index - keep_count + 1)
                
                # Episodes before the keep block are leaving
                episodes_with_files = [ep for ep in sorted_episodes if ep.get('hasFile')]
                
                for ep in episodes_with_files:
                    ep_index = next((i for i, se in enumerate(sorted_episodes) if se['id'] == ep['id']), None)
                    if ep_index is not None and ep_index < keep_start_index:
                        episodes_leaving.append(ep)
                
                logger.info(f"Keep block: episodes {keep_start_index} to {last_watched_index}, {len(episodes_leaving)} episodes leaving")
        
        return episodes_leaving
        
    except Exception as e:
        logger.error(f"Error finding episodes leaving keep block: {str(e)}")
        return []

def process_episodes_for_webhook(series_id, season_number, episode_number, rule, series_title=None):
    """
    Clean webhook processing - ONLY handles real-time episode management.
    Grace cleanup happens separately during scheduled cleanup (every 6 hours).
    """
    try:
        logger.info(f"Processing webhook for series {series_id}: S{season_number}E{episode_number}")
        
        # Parse rule using dropdown format
        if 'get_type' in rule and 'get_count' in rule:
            get_type = rule.get('get_type', 'episodes')
            get_count = rule.get('get_count', 1)
            keep_type = rule.get('keep_type', 'episodes') 
            keep_count = rule.get('keep_count', 1)
        else:
            # Fall back to legacy conversion for old rules
            get_option, keep_watched = rule_to_legacy_params(rule)
            get_type, get_count = parse_legacy_value(get_option)
            keep_type, keep_count = parse_legacy_value(keep_watched)
        
        # UPDATE ACTIVITY DATE (includes season/episode info)
        update_activity_date(series_id, season_number, episode_number)
        
        # Get and unmonitor current episode if needed
        all_episodes = fetch_all_episodes(series_id)
        current_episode = next(
            (ep for ep in all_episodes 
             if ep['seasonNumber'] == season_number and ep['episodeNumber'] == episode_number), 
            None
        )
        
        if not current_episode:
            logger.error(f"Could not find current episode S{season_number}E{episode_number}")
            return
            
        if not rule.get('monitor_watched', True):
            unmonitor_episodes([current_episode['id']])
        
        # GET NEXT EPISODES using dropdown system
        next_episode_ids = fetch_next_episodes_dropdown(
            series_id, season_number, episode_number, get_type, get_count
        )
        
        if next_episode_ids:
            monitor_or_search_episodes(next_episode_ids, rule.get('action_option', 'monitor'), series_id, series_title, get_type)
            logger.info(f"Processed {len(next_episode_ids)} next episodes")
        
        # IMMEDIATE DELETION: Episodes leaving keep block (real-time cleanup)
        episodes_leaving_keep_block = find_episodes_leaving_keep_block(
            all_episodes, keep_type, keep_count, season_number, episode_number
        )
        
        if episodes_leaving_keep_block:
            # Filter out anchor episodes (S01E01) - never delete these
            episodes_to_delete = [ep for ep in episodes_leaving_keep_block if not is_anchor_episode(ep, series_id)]
            
            if episodes_to_delete:
                episode_file_ids = [ep['episodeFileId'] for ep in episodes_to_delete if 'episodeFileId' in ep]
                if episode_file_ids:
                    delete_episodes_immediately(
                        episode_file_ids, 
                        series_title or f"Series {series_id}",
                        reason=f"Keep Rule (keeping {keep_count} {keep_type})"
                    )
                    logger.info(f"Immediately deleted {len(episode_file_ids)} episodes leaving keep block")
            
            # Log if we protected any anchors
            protected_anchors = [ep for ep in episodes_leaving_keep_block if is_anchor_episode(ep, series_id)]
            if protected_anchors:
                for ep in protected_anchors:
                    logger.info(f"🔒 Protected anchor S{ep.get('seasonNumber')}E{ep.get('episodeNumber')} from keep rule deletion")
        
        logger.info(f"✅ Webhook processing complete for {series_id}")
            
    except Exception as e:
        logger.error(f"Error in webhook processing: {str(e)}")
# ============================================================================
# GLOBAL SETTINGS & STORAGE GATE
# ============================================================================

def load_global_settings():
    """Load global settings including storage gate."""
    try:
        settings_path = os.path.join(os.getcwd(), 'config', 'global_settings.json')
        
        if os.path.exists(settings_path):
            with open(settings_path, 'r') as f:
                settings = json.load(f)
            
            # ADD THIS MIGRATION BLOCK:
            # MIGRATION: Add dry_run_mode if missing (default to True for safety)
            if 'dry_run_mode' not in settings:
                settings['dry_run_mode'] = True
                save_global_settings(settings)
                logger.info("✓ Migrated global_settings.json - added dry_run_mode: true")
            
            
            
            return settings
        else:
            # Default settings (already has dry_run_mode: True - good!)
            default_settings = {
                'global_storage_min_gb': None,
                'cleanup_interval_hours': 6,
                'dry_run_mode': True,
                'auto_assign_new_series': False,
                
                'notifications_enabled': False,
                'discord_webhook_url': '',
                'episeerr_url': 'http://localhost:5002'
            }
            save_global_settings(default_settings)
            return default_settings
    except Exception as e:
        logger.error(f"Error loading global settings: {str(e)}")
        return {
            'global_storage_min_gb': None,
            'cleanup_interval_hours': 6,
            'dry_run_mode': True,
            'auto_assign_new_series': False,
            'notifications_enabled': False,
            'discord_webhook_url': '',
            'episeerr_url': 'http://localhost:5002'
        }

def save_global_settings(settings):
    """Save global settings to file with automatic backup."""
    try:
        settings_path = os.path.join(os.getcwd(), 'config', 'global_settings.json')
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        
        # Backup BEFORE saving (only when actually writing)
        if os.path.exists(settings_path):
            backup_path = settings_path + '.bak'
            try:
                shutil.copy2(settings_path, backup_path)
                logger.debug(f"Backed up global_settings.json to {backup_path}")
            except Exception as e:
                logger.warning(f"Could not backup global_settings.json: {e}")
        
        # Save the settings
        with open(settings_path, 'w') as f:
            json.dump(settings, f, indent=4)
        logger.info("Global settings saved successfully")
    except Exception as e:
        logger.error(f"Error saving global settings: {str(e)}")

def check_global_storage_gate():
    """Check if global storage gate allows cleanup to proceed."""
    try:
        global_settings = load_global_settings()
        storage_min_gb = global_settings.get('global_storage_min_gb')
        
        if not storage_min_gb:
            # No storage gate configured - always allow cleanup
            return True, None, "No global storage gate - cleanup always enabled"
        
        disk_info = get_sonarr_disk_space()
        if not disk_info:
            return False, storage_min_gb, "Could not get disk space information"
        
        current_free_gb = disk_info['free_space_gb']
        
        if current_free_gb < storage_min_gb:
            return True, storage_min_gb, f"Storage gate OPEN: {current_free_gb:.1f}GB < {storage_min_gb}GB threshold"
        else:
            return False, storage_min_gb, f"Storage gate CLOSED: {current_free_gb:.1f}GB >= {storage_min_gb}GB threshold"
        
    except Exception as e:
        logger.error(f"Error checking global storage gate: {str(e)}")
        return False, None, f"Storage gate error: {str(e)}"
    
def check_time_based_cleanup(series_id, rule):
    """
    Debug function for episeerr.py test routes.
    Production cleanup uses the 3 separate functions instead.
    """
    try:
        grace_watched = rule.get('grace_watched')
        grace_unwatched = rule.get('grace_unwatched')
        dormant_days = rule.get('dormant_days')
        
        if not any([grace_watched, grace_unwatched, dormant_days]):
            return False, "No time-based cleanup configured"
        
        # Get activity date for basic check
        activity_date = get_activity_date_with_hierarchy(series_id)
        
        if not activity_date:
            return False, "No activity date available"
        
        current_time = int(time.time())
        days_since_activity = (current_time - activity_date) / (24 * 60 * 60)
        
        # Simple check - just report what would happen
        if dormant_days and days_since_activity > dormant_days:
            return True, f"DORMANT: {days_since_activity:.1f}d > {dormant_days}d"
        elif grace_watched and days_since_activity > grace_watched:
            return True, f"GRACE WATCHED: {days_since_activity:.1f}d > {grace_watched}d"
        else:
            return False, f"PROTECTED: {days_since_activity:.1f}d since activity"
            
    except Exception as e:
        return False, f"Error: {str(e)}"

def is_dry_run_enabled(rule_name=None):
    """Check if dry run is enabled (simplified version)."""
    # Check environment variable first
    if os.getenv('CLEANUP_DRY_RUN', 'false').lower() == 'true':
        return True
    
    # Check rule-specific setting (you'll need to implement this based on your setup)
    # For now, return False
    return False
def is_protected_by_expression(season_num, episode_num, expression, total_seasons=None):
    """
    Check if a season/episode matches an always_have expression.

    Syntax (comma-separated for multiple):
      all        - every episode
      s1         - all episodes of season 1
      s1e1       - season 1 episode 1 only
      s1e1-5     - season 1 episodes 1 through 5
      s1-3       - all episodes of seasons 1 through 3
      s*e1       - episode 1 of every season (wildcard expands with total_seasons)
    """
    if not expression:
        return False

    expression = expression.strip().lower()
    if not expression:
        return False

    for part in expression.split(','):
        part = part.strip()
        if not part:
            continue

        if part == 'all':
            return True

        if not part.startswith('s'):
            continue

        # Wildcard season: s*eN
        if part.startswith('s*'):
            remainder = part[2:]
            if remainder.startswith('e'):
                try:
                    if episode_num == int(remainder[1:]):
                        return True
                except ValueError:
                    pass
            continue

        rest = part[1:]  # strip leading 's'

        if 'e' in rest:
            # s<season>e<ep> or s<season>e<ep_start>-<ep_end>
            e_idx = rest.index('e')
            try:
                target_season = int(rest[:e_idx])
            except ValueError:
                continue
            if season_num != target_season:
                continue
            ep_part = rest[e_idx + 1:]
            if '-' in ep_part:
                bounds = ep_part.split('-', 1)
                try:
                    if int(bounds[0]) <= episode_num <= int(bounds[1]):
                        return True
                except ValueError:
                    pass
            else:
                try:
                    if episode_num == int(ep_part):
                        return True
                except ValueError:
                    pass
        elif '-' in rest:
            # s<start>-<end>: season range, all episodes
            bounds = rest.split('-', 1)
            try:
                if int(bounds[0]) <= season_num <= int(bounds[1]):
                    return True
            except ValueError:
                pass
        else:
            # s<N>: entire season
            try:
                if season_num == int(rest):
                    return True
            except ValueError:
                pass

    return False


def process_always_have(series_id, expression):
    """
    Monitor any episodes matching the always_have expression and trigger a search.
    Only ever adds monitoring - never unmonitors or deletes anything.

    Args:
        series_id: Sonarr series ID (int)
        expression: always_have expression string (e.g. 's1', 's*e1', 'all')
    """
    if not expression or not expression.strip():
        return

    headers = {'X-Api-Key': SONARR_API_KEY}

    try:
        resp = requests.get(
            f"{SONARR_URL}/api/v3/episode?seriesId={series_id}",
            headers=headers
        )
        if not resp.ok:
            logger.error(f"process_always_have: failed to get episodes for series {series_id}: {resp.text}")
            return

        all_episodes = resp.json()

        # Collect unmonitored episodes that match the expression
        to_monitor = []
        for ep in all_episodes:
            season = ep.get('seasonNumber', 0)
            ep_num = ep.get('episodeNumber', 0)
            if season == 0:
                continue  # skip specials
            if is_protected_by_expression(season, ep_num, expression):
                if not ep.get('monitored', False):
                    to_monitor.append(ep['id'])

        if not to_monitor:
            logger.info(
                f"process_always_have: no unmonitored matches for series {series_id} "
                f"with expression '{expression}'"
            )
            return

        # Monitor matching episodes
        monitor_resp = requests.put(
            f"{SONARR_URL}/api/v3/episode/monitor",
            headers=headers,
            json={"episodeIds": to_monitor, "monitored": True}
        )
        if not monitor_resp.ok:
            logger.error(
                f"process_always_have: failed to monitor episodes for series {series_id}: "
                f"{monitor_resp.text}"
            )
            return

        logger.info(
            f"process_always_have: monitored {len(to_monitor)} episodes for series {series_id} "
            f"(always_have: '{expression}')"
        )

        # Trigger search for the newly monitored episodes
        search_resp = requests.post(
            f"{SONARR_URL}/api/v3/command",
            headers=headers,
            json={"name": "EpisodeSearch", "episodeIds": to_monitor}
        )
        if search_resp.ok:
            logger.info(f"process_always_have: triggered search for {len(to_monitor)} episodes")
        else:
            logger.error(f"process_always_have: search trigger failed: {search_resp.text}")

    except Exception as e:
        logger.error(f"process_always_have: error for series {series_id}: {e}", exc_info=True)


def is_anchor_episode(episode, series_id=None, check_always_have=True):
    """
    Check if an episode is an "anchor" that should never be deleted.

    Anchor episodes are protected from ALL cleanup operations:
    - Grace period cleanup (watched/unwatched)
    - Keep rule cleanup (seasons/episodes)
    - Manual unmonitor operations

    NOTE: pass check_always_have=False from dormant cleanup - dormant is
    intentionally nuclear and should not respect always_have expressions.

    Currently anchored:
    - S01E01 (pilot episode) - IF rule-level 'keep_pilot' is enabled
    - Any episode matching the rule's 'always_have' expression

    Args:
        episode: Episode dict with 'seasonNumber' and 'episodeNumber'
        series_id: Optional series ID to check rule-based protection
        check_always_have: Whether to evaluate the always_have expression (default True)

    Returns:
        bool: True if episode should be protected from deletion
    """
    season = episode.get('seasonNumber')
    episode_num = episode.get('episodeNumber')

    is_pilot = (season == 1 and episode_num == 1)

    if series_id is not None:
        from episeerr import load_config, get_rule_for_series
        config = load_config()
        rule = get_rule_for_series(series_id, config)

        if rule:
            # keep_pilot: protect S01E01
            if is_pilot and rule.get('keep_pilot', False):
                return True

            # always_have expression (skipped for dormant cleanup)
            if check_always_have:
                always_have = rule.get('always_have', '')
                if always_have and is_protected_by_expression(season, episode_num, always_have):
                    return True

    return False


def delete_episodes_immediately(episode_file_ids, series_title, reason="Keep Rule"):
    """
    Direct deletion for Keep rule - NO dry run, NO approval queue.
    Used by: process_episodes_for_webhook() for real-time Keep rule deletions.
    """
    if not episode_file_ids:
        return
    
    logger.info(f"🗑️ KEEP RULE: Deleting {len(episode_file_ids)} episodes from {series_title} - {reason}")
    
    headers = {'X-Api-Key': SONARR_API_KEY}
    successful_deletes = 0
    failed_deletes = []
    
    for episode_file_id in episode_file_ids:
        try:
            url = f"{SONARR_URL}/api/v3/episodeFile/{episode_file_id}"
            response = requests.delete(url, headers=headers)
            response.raise_for_status()
            successful_deletes += 1
            logger.info(f"✅ Deleted episode file ID: {episode_file_id}")
        except Exception as err:
            failed_deletes.append(episode_file_id)
            logger.error(f"❌ Failed to delete episode file {episode_file_id}: {err}")
    
    logger.info(f"📊 Keep rule deletion: {successful_deletes} successful, {len(failed_deletes)} failed")
    if failed_deletes:
        logger.error(f"❌ Failed deletes: {failed_deletes}")
def delete_episodes_in_sonarr_with_logging(
    episode_file_ids, 
    rule_dry_run,  # Renamed from is_dry_run for clarity
    series_title,
    reason=None,
    date_source=None,
    date_value=None,
    rule_name=None):
    """
    Delete episodes with approval queue for Grace/Dormant cleanup.
    Respects BOTH global dry_run_mode AND rule-level dry_run (either triggers queue).
    
    Args:
        episode_file_ids: List of Sonarr episode file IDs to delete
        rule_dry_run: Rule-level dry_run setting (from rule config)
        series_title: Name of the series
        reason: Explanation for deletion (e.g., "Grace Period - Watched (10 days)")
        date_source: Where the date came from (e.g., "Tautulli", "Sonarr")
        date_value: The date used in decision (e.g., "2025-06-21")
        rule_name: Name of the rule triggering deletion
    """
    if not episode_file_ids:
        return
    
    # Check BOTH global dry_run_mode AND rule-level dry_run
    global_settings = load_global_settings()
    global_dry_run = global_settings.get('dry_run_mode', False)
    
    # If EITHER is true, use dry run
    is_dry_run = global_dry_run or rule_dry_run
    
    if is_dry_run:  # ✅ KEEP THIS ONE
        if global_dry_run and rule_dry_run:
            cleanup_logger.info(f"🔍 DRY RUN (global + rule): Queueing {len(episode_file_ids)} episodes")
        elif global_dry_run:
            cleanup_logger.info(f"🔍 DRY RUN (global): Queueing {len(episode_file_ids)} episodes")
        else:
            cleanup_logger.info(f"🔍 DRY RUN (rule '{rule_name}'): Queueing {len(episode_file_ids)} episodes")
        
        # Import here to avoid circular imports
        from pending_deletions import queue_deletion
        
        headers = {'X-Api-Key': SONARR_API_KEY}
        
        for episode_file_id in episode_file_ids:
            try:
                # Get episode file details
                ep_url = f"{SONARR_URL}/api/v3/episodefile/{episode_file_id}"
                ep_response = requests.get(ep_url, headers=headers, timeout=10)
                
                if not ep_response.ok:
                    cleanup_logger.warning(f"Could not fetch details for episode file {episode_file_id}")
                    continue
                
                ep_data = ep_response.json()
                series_id = ep_data.get('seriesId')
                season_num = ep_data.get('seasonNumber')
                
                # Get series title
                series_response = requests.get(f"{SONARR_URL}/api/v3/series/{series_id}", headers=headers, timeout=10)
                series_title_full = series_response.json().get('title', series_title) if series_response.ok else series_title
                
                # Get episode details
                episode_data_list = ep_data.get('episodes', [])
                if episode_data_list:
                    first_ep = episode_data_list[0]
                    episode_id = first_ep.get('id')
                    episode_num = first_ep.get('episodeNumber', 0)
                    episode_title = first_ep.get('title', f"Episode {episode_num}")
                else:
                    # Fallback if episodes array is empty
                    episode_id = None
                    episode_num = 0
                    episode_title = f"S{season_num}E{episode_num}"
                
                # Queue for deletion approval with CORRECT parameters
                queue_deletion(
                    series_id=series_id,
                    series_title=series_title_full,
                    season_number=season_num,
                    episode_number=episode_num,
                    episode_id=episode_id,
                    episode_title=episode_title,
                    episode_file_id=episode_file_id,
                    reason=reason or "Cleanup",
                    date_source=date_source or "Unknown",
                    date_value=date_value or "N/A",
                    rule_name=rule_name or "Unknown",
                    file_size=ep_data.get('size', 0)
                )
                    
            except Exception as e:
                cleanup_logger.error(f"Error queueing episode {episode_file_id}: {str(e)}")
        
        cleanup_logger.info(f"✅ Queued {len(episode_file_ids)} episodes for approval")
        return
    
    # LIVE DELETION (both global and rule dry_run are False)
    cleanup_logger.info(f"🗑️  DELETING: {len(episode_file_ids)} episode files from {series_title}")
    
    headers = {'X-Api-Key': SONARR_API_KEY}
    successful_deletes = 0
    failed_deletes = []
    
    for episode_file_id in episode_file_ids:
        try:
            url = f"{SONARR_URL}/api/v3/episodeFile/{episode_file_id}"
            response = requests.delete(url, headers=headers)
            response.raise_for_status()
            successful_deletes += 1
            cleanup_logger.info(f"✅ Deleted episode file ID: {episode_file_id}")
        except Exception as err:
            failed_deletes.append(episode_file_id)
            cleanup_logger.error(f"❌ Failed to delete episode file {episode_file_id}: {err}")
    
    cleanup_logger.info(f"📊 Deletion summary: {successful_deletes} successful, {len(failed_deletes)} failed")
    if failed_deletes:
        cleanup_logger.error(f"❌ Failed deletes: {failed_deletes}")



# ============================================================================
# CLEANUP FUNCTIONS - Your new simplified 3-function system
# ============================================================================

def run_grace_watched_cleanup():
    """
    Grace Watched Cleanup - Keep last watched episode as reference point.
    
    NEW BEHAVIOR:
    - Deletes all watched episodes EXCEPT the last one
    - Last watched episode = reference point to catch up from
    - Does NOT apply Get rule (that's only for watch webhooks)
    - Does NOT update activity_date (preserves real watch timestamp)
    """
    try:
        cleanup_logger.info("🟡 GRACE WATCHED CLEANUP: Checking inactive series")
        
        config = load_config()
        
        # MASTER SAFETY SWITCH
        global_dry_run_config = config.get('dry_run_mode', False)
        global_dry_run_env = os.getenv('CLEANUP_DRY_RUN', 'false').lower() == 'true'
        global_dry_run = global_dry_run_config or global_dry_run_env
        
        if global_dry_run:
            cleanup_logger.info("🛡️ Global dry run mode ENABLED - all deletions will be queued for approval")
        
        total_deleted = 0
        headers = {'X-Api-Key': SONARR_API_KEY}
        response = requests.get(f"{SONARR_URL}/api/v3/series", headers=headers)
        all_series = response.json() if response.ok else []
        current_time = int(time.time())
        
        for rule_name, rule in config['rules'].items():
            grace_watched_days = rule.get('grace_watched')
            if not grace_watched_days:
                continue
            
            cleanup_logger.info(f"📋 Rule '{rule_name}': grace_watched={grace_watched_days}d")
            
            # Apply master safety switch
            rule_dry_run = rule.get('dry_run', False)
            if global_dry_run:
                is_dry_run = True
                cleanup_logger.info(f"   🛡️ Global dry run enforced")
            else:
                is_dry_run = rule_dry_run
            
            series_dict = rule.get('series', {})
            for series_id_str, series_data in series_dict.items():
                try:
                    series_id = int(series_id_str)
                    series_info = next((s for s in all_series if s['id'] == series_id), None)
                    if not series_info:
                        continue
                    
                    series_title = series_info['title']
                    
                    # CHECK: Already cleaned?
                    if isinstance(series_data, dict) and series_data.get('grace_cleaned', False):
                        cleanup_logger.debug(f"⏭️ {series_title}: Already cleaned, skipping")
                        continue
                    
                    # Get activity date
                    result = get_activity_date_with_hierarchy(series_id, series_title, return_complete=True)
                    if isinstance(result, tuple) and len(result) == 3:
                        activity_date, last_season, last_episode = result
                    else:
                        activity_date = result
                        last_season, last_episode = 1, 1
                    
                    if not activity_date:
                        cleanup_logger.debug(f"⏭️ {series_title}: No activity date, skipping")
                        continue
                    
                    days_since_activity = (current_time - activity_date) / (24 * 60 * 60)
                    
                    if days_since_activity > grace_watched_days:
                        cleanup_logger.info(f"🟡 {series_title}: Inactive {days_since_activity:.1f}d > {grace_watched_days}d")
                        cleanup_logger.info(f"   📺 Last watched: S{last_season}E{last_episode}")
                        
                        # Get all episodes
                        all_episodes = fetch_all_episodes(series_id)
                        
                        # Find watched episodes
                        watched_episodes = []
                        for episode in all_episodes:
                            if not episode.get('hasFile'):
                                continue
                            season_num = episode.get('seasonNumber', 0)
                            episode_num = episode.get('episodeNumber', 0)
                            
                            if (season_num < last_season or 
                                (season_num == last_season and episode_num <= last_episode)):
                                watched_episodes.append(episode)
                        
                        # Sort by season/episode
                        watched_episodes.sort(key=lambda ep: (ep['seasonNumber'], ep['episodeNumber']))
                        
                        if len(watched_episodes) > 1:
                            # Keep last watched, delete rest
                            keep_episode = watched_episodes[-1]
                            delete_episodes = watched_episodes[:-1]
                            
                            # Filter out anchor episodes (S01E01)
                            delete_episodes = [ep for ep in delete_episodes if not is_anchor_episode(ep, series_id)]
                            
                            episode_file_ids = [ep['episodeFileId'] for ep in delete_episodes if 'episodeFileId' in ep]
                            
                            if episode_file_ids:
                                cleanup_logger.info(f"   📊 Deleting {len(episode_file_ids)} old watched episodes")
                                cleanup_logger.info(f"   🔖 Keeping S{keep_episode['seasonNumber']}E{keep_episode['episodeNumber']} as reference")
                                
                                from datetime import datetime
                                activity_date_str = datetime.fromtimestamp(activity_date).strftime('%Y-%m-%d')
                                
                                delete_episodes_in_sonarr_with_logging(
                                    episode_file_ids,
                                    is_dry_run,
                                    series_title,
                                    reason=f"Grace Watched ({grace_watched_days}d) - Keep Last Watched",
                                    date_source="Last Activity",
                                    date_value=activity_date_str,
                                    rule_name=rule_name
                                )
                                total_deleted += len(episode_file_ids)
                        elif len(watched_episodes) == 1:
                            cleanup_logger.info(f"   🔖 Only 1 watched episode - keeping as reference")
                        else:
                            cleanup_logger.info(f"   ⏭️ No watched episodes to delete")
                        
                        # Mark as cleaned (unwatched cleanup will verify bookmark exists)
                        # Don't mark here - let unwatched cleanup decide
                        
                    else:
                        cleanup_logger.debug(f"🛡️ {series_title}: Protected - {days_since_activity:.1f}d since activity")
                
                except (ValueError, TypeError) as e:
                    cleanup_logger.error(f"Error processing series {series_id_str}: {str(e)}")
                    continue
        
        cleanup_logger.info(f"🟡 Grace watched cleanup: Deleted {total_deleted} episodes")
        return total_deleted
        
    except Exception as e:
        cleanup_logger.error(f"Error in grace_watched cleanup: {str(e)}")
        return 0


# ==============================================================================
# REPLACE run_grace_unwatched_cleanup() WITH THIS
# ==============================================================================

def run_grace_unwatched_cleanup():
    """
    Grace Unwatched Cleanup - Keep first unwatched as bookmark.
    
    NEW BEHAVIOR:
    - Keeps first unwatched episode (after last watched) as bookmark
    - Deletes all other unwatched episodes
    - Marks series as cleaned if bookmark exists
    - Keeps checking if no next episode exists yet (waits for grab webhook)
    """
    try:
        cleanup_logger.info("⏰ GRACE UNWATCHED CLEANUP: Checking inactive series")
        
        config = load_config()
        
        # MASTER SAFETY SWITCH
        global_dry_run_config = config.get('dry_run_mode', False)
        global_dry_run_env = os.getenv('CLEANUP_DRY_RUN', 'false').lower() == 'true'
        global_dry_run = global_dry_run_config or global_dry_run_env
        
        if global_dry_run:
            cleanup_logger.info("🛡️ Global dry run mode ENABLED - all deletions will be queued for approval")
        
        total_deleted = 0
        headers = {'X-Api-Key': SONARR_API_KEY}
        response = requests.get(f"{SONARR_URL}/api/v3/series", headers=headers)
        all_series = response.json() if response.ok else []
        current_time = int(time.time())
        
        for rule_name, rule in config['rules'].items():
            grace_unwatched_days = rule.get('grace_unwatched')
            if not grace_unwatched_days:
                continue
            
            cleanup_logger.info(f"📋 Rule '{rule_name}': grace_unwatched={grace_unwatched_days}d")
            
            # Apply master safety switch
            rule_dry_run = rule.get('dry_run', False)
            if global_dry_run:
                is_dry_run = True
                cleanup_logger.info(f"   🛡️ Global dry run enforced")
            else:
                is_dry_run = rule_dry_run
            
            series_dict = rule.get('series', {})
            for series_id_str, series_data in series_dict.items():
                try:
                    series_id = int(series_id_str)
                    series_info = next((s for s in all_series if s['id'] == series_id), None)
                    if not series_info:
                        continue
                    
                    series_title = series_info['title']
                    
                    # CHECK: Already cleaned?
                    if isinstance(series_data, dict) and series_data.get('grace_cleaned', False):
                        cleanup_logger.debug(f"⏭️ {series_title}: Already cleaned, skipping")
                        continue
                    
                    # Get activity date
                    result = get_activity_date_with_hierarchy(series_id, series_title, return_complete=True)
                    if isinstance(result, tuple) and len(result) == 3:
                        activity_date, last_season, last_episode = result
                    else:
                        activity_date = result
                        last_season, last_episode = 1, 1
                    
                    if not activity_date:
                        cleanup_logger.debug(f"⏭️ {series_title}: No activity date, skipping")
                        continue
                    
                    days_since_activity = (current_time - activity_date) / (24 * 60 * 60)
                    
                    if days_since_activity > grace_unwatched_days:
                        cleanup_logger.info(f"⏰ {series_title}: Inactive {days_since_activity:.1f}d > {grace_unwatched_days}d")
                        cleanup_logger.info(f"   📺 Last watched: S{last_season}E{last_episode}")
                        
                        # Get all episodes
                        all_episodes = fetch_all_episodes(series_id)
                        
                        # Find unwatched episodes (AFTER last watched)
                        unwatched_episodes = []
                        for episode in all_episodes:
                            if not episode.get('hasFile'):
                                continue
                            season_num = episode.get('seasonNumber', 0)
                            episode_num = episode.get('episodeNumber', 0)
                            
                            if (season_num > last_season or 
                                (season_num == last_season and episode_num > last_episode)):
                                unwatched_episodes.append(episode)
                        
                        # Sort by season/episode
                        unwatched_episodes.sort(key=lambda ep: (ep['seasonNumber'], ep['episodeNumber']))
                        
                        if len(unwatched_episodes) > 1:
                            # Keep first unwatched, delete rest
                            bookmark_episode = unwatched_episodes[0]
                            delete_episodes = unwatched_episodes[1:]
                            
                            # Filter out anchor episodes (S01E01)
                            delete_episodes = [ep for ep in delete_episodes if not is_anchor_episode(ep, series_id)]
                            
                            episode_file_ids = [ep['episodeFileId'] for ep in delete_episodes if 'episodeFileId' in ep]
                            
                            if episode_file_ids:
                                cleanup_logger.info(f"   📊 Deleting {len(episode_file_ids)} extra unwatched episodes")
                                cleanup_logger.info(f"   🔖 Keeping S{bookmark_episode['seasonNumber']}E{bookmark_episode['episodeNumber']} as bookmark")
                                
                                from datetime import datetime
                                activity_date_str = datetime.fromtimestamp(activity_date).strftime('%Y-%m-%d')
                                
                                delete_episodes_in_sonarr_with_logging(
                                    episode_file_ids,
                                    is_dry_run,
                                    series_title,
                                    reason=f"Grace Unwatched ({grace_unwatched_days}d) - Keep First Unwatched",
                                    date_source="Last Activity",
                                    date_value=activity_date_str,
                                    rule_name=rule_name
                                )
                                total_deleted += len(episode_file_ids)
                            
                            # Mark as cleaned - has bookmark
                            if isinstance(series_data, dict):
                                series_data['grace_cleaned'] = True
                                save_config(config)
                                cleanup_logger.info(f"   ✅ Bookmark established - marked as cleaned")
                        
                        elif len(unwatched_episodes) == 1:
                            cleanup_logger.info(f"   🔖 Has 1 unwatched episode as bookmark")
                            # Mark as cleaned - already has bookmark
                            if isinstance(series_data, dict):
                                series_data['grace_cleaned'] = True
                                save_config(config)
                                cleanup_logger.info(f"   ✅ Bookmark exists - marked as cleaned")
                        
                        else:
                            cleanup_logger.info(f"   ⏭️ No unwatched episodes - waiting for next episode")
                            cleanup_logger.info(f"   🔄 Will keep checking until grab webhook")
                            # DON'T mark as cleaned - keep checking
                    
                    else:
                        cleanup_logger.debug(f"🛡️ {series_title}: Protected - {days_since_activity:.1f}d since activity")
                
                except (ValueError, TypeError) as e:
                    cleanup_logger.error(f"Error processing series {series_id_str}: {str(e)}")
                    continue
        
        cleanup_logger.info(f"⏰ Grace unwatched cleanup: Deleted {total_deleted} episodes")
        return total_deleted
        
    except Exception as e:
        cleanup_logger.error(f"Error in grace_unwatched cleanup: {str(e)}")
        return 0
    

# UPDATED DORMANT CLEANUP WITH MASTER SAFETY SWITCH
# Matches the same safety logic as grace watched/unwatched

def run_dormant_cleanup():
    """
    Process dormant cleanup with optional storage gate and MASTER SAFETY SWITCH.
    
    NOTE: Dormant cleanup ALWAYS uses series-wide activity (not per-season),
    as it's meant to detect completely abandoned shows.
    
    MASTER SAFETY: Global dry_run_mode=true overrides all rule settings.
    """
    try:
        cleanup_logger.info("🔴 DORMANT CLEANUP: Checking abandoned series")
        
        config = load_config()
        global_settings = load_global_settings()
        
        # MASTER SAFETY SWITCH: Check global dry_run from config.json
        global_dry_run_config = global_settings.get('dry_run_mode', False)
        
        # Also check environment variable
        global_dry_run_env = os.getenv('CLEANUP_DRY_RUN', 'false').lower() == 'true'
        
        # If EITHER is true, enforce dry run globally
        global_dry_run = global_dry_run_config or global_dry_run_env
        
        if global_dry_run:
            cleanup_logger.info("🛡️ Global dry run mode ENABLED - all deletions will be queued for approval")
        
        # Check storage gate
        storage_min_gb = global_settings.get('global_storage_min_gb')
        if storage_min_gb:
            gate_open, _, gate_reason = check_global_storage_gate()
            if not gate_open:
                cleanup_logger.info(f"🔒 Storage gate CLOSED: {gate_reason}")
                return 0
            cleanup_logger.info(f"🔓 Storage gate OPEN: {gate_reason}")
        else:
            cleanup_logger.info("⏰ No storage gate - running scheduled dormant cleanup")
        
        # Get candidates
        candidates = []
        headers = {'X-Api-Key': SONARR_API_KEY}
        all_series = requests.get(f"{SONARR_URL}/api/v3/series", headers=headers).json()
        current_time = int(time.time())
        
        for rule_name, rule in config['rules'].items():
            dormant_days = rule.get('dormant_days')
            if not dormant_days:
                continue
            
            cleanup_logger.info(f"📋 Rule '{rule_name}': dormant={dormant_days}d (always uses series-wide activity)")
            
            # Apply master safety switch
            rule_dry_run = rule.get('dry_run', False)
            if global_dry_run:
                is_dry_run = True  # Global override - ALWAYS dry run
                cleanup_logger.info(f"   🛡️ Global dry run enforced (rule setting ignored)")
            else:
                is_dry_run = rule_dry_run  # Use rule-specific setting
                if is_dry_run:
                    cleanup_logger.info(f"   🔍 Rule-level dry run enabled")
            
            series_dict = rule.get('series', {})
            for series_id_str, series_data in series_dict.items():
                try:
                    series_id = int(series_id_str)
                    series_info = next((s for s in all_series if s['id'] == series_id), None)
                    if not series_info:
                        continue
                    
                    # ALWAYS use series-wide activity for dormant (not per-season)
                    # This ensures we only delete shows that are completely abandoned
                    if isinstance(series_data, dict):
                        activity_date = series_data.get('activity_date')
                    else:
                        activity_date = None
                    
                    # Fallback to hierarchy if no config activity
                    if not activity_date:
                        activity_date = get_activity_date_with_hierarchy(series_id, series_info['title'])
                    
                    if not activity_date:
                        continue
                    
                    days_since_activity = (current_time - activity_date) / (24 * 60 * 60)
                    if days_since_activity > dormant_days:
                        all_episodes = fetch_all_episodes(series_id)
                        # Dormant cleanup is nuclear: respect keep_pilot but NOT always_have
                        deletable_episodes = [ep for ep in all_episodes if ep.get('hasFile') and 'episodeFileId' in ep and not is_anchor_episode(ep, series_id, check_always_have=False)]
                        episode_file_ids = [ep['episodeFileId'] for ep in deletable_episodes]
                        
                        if episode_file_ids:
                            candidates.append({
                                'series_id': series_id,
                                'title': series_info['title'],
                                'days_since_activity': days_since_activity,
                                'episode_file_ids': episode_file_ids,
                                'is_dry_run': is_dry_run,
                                'last_activity': activity_date,
                                'rule_name': rule_name
                            })
                            
                except (ValueError, TypeError):
                    continue
        
        # Process candidates
        processed_count = 0
        candidates.sort(key=lambda x: x['days_since_activity'], reverse=True)
        
        for candidate in candidates:
            # Check storage gate again if configured
            if storage_min_gb and not candidate['is_dry_run']:
                current_disk = get_sonarr_disk_space()
                if current_disk and current_disk['free_space_gb'] >= storage_min_gb:
                    cleanup_logger.info(f"🎯 Storage target reached")
                    break
            
            cleanup_logger.info(f"🔴 {candidate['title']}: Dormant for {candidate['days_since_activity']:.1f} days")
            from datetime import datetime
            
            # Format the last activity date
            if candidate.get('last_activity'):
                activity_date = datetime.fromtimestamp(candidate['last_activity']).strftime('%Y-%m-%d')
                date_source = "Tautulli"
            else:
                activity_date = "Unknown"
                date_source = "No Activity Data"
            
            delete_episodes_in_sonarr_with_logging(
                candidate['episode_file_ids'], 
                candidate['is_dry_run'], 
                candidate['title'],
                reason=f"Dormant Series ({candidate['days_since_activity']:.1f} days inactive)",
                date_source=date_source,
                date_value=activity_date,
                rule_name=candidate.get('rule_name', 'dormant')
            )
            processed_count += 1
        
        cleanup_logger.info(f"🔴 Dormant cleanup: Processed {processed_count} series")
        return processed_count
        
    except Exception as e:
        cleanup_logger.error(f"Error in dormant cleanup: {str(e)}")
        return 0



def get_sonarr_disk_space():
    """Get disk space information from Sonarr."""
    try:
        headers = {'X-Api-Key': SONARR_API_KEY}
        response = requests.get(f"{SONARR_URL}/api/v3/diskspace", headers=headers)
        if response.ok:
            diskspace_data = response.json()
            
            if diskspace_data:
                main_disk = max(diskspace_data, key=lambda x: x.get('totalSpace', 0))
                
                total_space_bytes = main_disk.get('totalSpace', 0)
                free_space_bytes = main_disk.get('freeSpace', 0)
                
                return {
                    'total_space_gb': round(total_space_bytes / (1024**3), 1),
                    'free_space_gb': round(free_space_bytes / (1024**3), 1),
                    'path': main_disk.get('path', 'Unknown')
                }
        return None
    except Exception as e:
        logger.error(f"Error getting disk space: {str(e)}")
        return None

def run_unified_cleanup():
    """
    UNIFIED CLEANUP: Uses your 3 existing functions with smart storage logic
    
    LOGIC:
    - No storage gate → Always run all 3 functions (manual/scheduled)
    - Storage gate set → Only run if below threshold
    - Priority order: dormant → grace_watched → grace_unwatched
    - Stop when back above threshold
    """
    try:
        cleanup_logger.info("=" * 80)
        cleanup_logger.info("🚀 STARTING UNIFIED CLEANUP")
        
        global_settings = load_global_settings()
        storage_min_gb = global_settings.get('global_storage_min_gb')
        
        # Check storage gate
        if storage_min_gb:
            # Storage gate is SET - check if we need to clean
            current_disk = get_sonarr_disk_space()
            if not current_disk:
                cleanup_logger.error("❌ Cannot get disk space - aborting cleanup")
                return 0
            
            if current_disk['free_space_gb'] >= storage_min_gb:
                cleanup_logger.info(f"🔒 Storage gate CLOSED: {current_disk['free_space_gb']:.1f}GB >= {storage_min_gb}GB threshold")
                cleanup_logger.info("✅ No cleanup needed")
                return 0
            
            cleanup_logger.info(f"🔓 Storage gate OPEN: {current_disk['free_space_gb']:.1f}GB < {storage_min_gb}GB threshold")
            cleanup_logger.info(f"🎯 Target: Clean until back above {storage_min_gb}GB")
            storage_gated = True
        else:
            # No storage gate - always run
            cleanup_logger.info("⏰ No storage gate - running all cleanup functions")
            storage_gated = False
        
        # ==================== NEW: PHASE 0 - TAG RECONCILIATION ====================
        cleanup_logger.info("=" * 80)
        cleanup_logger.info("🏷️  Phase 0: Tag reconciliation (drift + orphaned)")
        try:
            config = load_config()
            drift_fixed = 0
            drift_synced = 0
            
            # Step 1: Drift detection - fix mismatched tags
            for rule_name, rule_details in config['rules'].items():
                for series_id_str in list(rule_details.get('series', {}).keys()):
                    try:
                        series_id = int(series_id_str)
                        matches, actual_tag_rule = validate_series_tag(series_id, rule_name)
                        
                        if not matches:
                            if actual_tag_rule:
                                # Drift detected: tag changed in Sonarr
                                cleanup_logger.warning(f"⚠️  DRIFT: Series {series_id} config={rule_name}, tag={actual_tag_rule}")
                                
                                # Move series to new rule
                                series_data = rule_details['series'][series_id_str]
                                del rule_details['series'][series_id_str]
                                
                                # Find actual rule name (case-insensitive)
                                actual_rule_name = None
                                for rn in config['rules'].keys():
                                    if rn.lower() == actual_tag_rule.lower():
                                        actual_rule_name = rn
                                        break

                                if actual_rule_name:
                                    target_rule = config['rules'][actual_rule_name]
                                    target_rule.setdefault('series', {})[series_id_str] = series_data
                                    drift_fixed += 1
                                    cleanup_logger.info(f"   ✓ Moved to '{actual_rule_name}' rule")
                                else:
                                    cleanup_logger.error(f"   ✗ Target rule '{actual_tag_rule}' not found")
                            else:
                                # No tag found: sync from config
                                sync_rule_tag_to_sonarr(series_id, rule_name)
                                drift_synced += 1
                                cleanup_logger.info(f"   ✓ Synced missing tag for series {series_id}")
                                
                    except Exception as e:
                        cleanup_logger.error(f"   ✗ Error checking series {series_id_str}: {str(e)}")
            
            # Step 2: Orphaned tags - find shows tagged in Sonarr but not in config
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
                tag_mapping = get_tag_mapping()
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
                                cleanup_logger.info(f"   ✓ ORPHANED: Added {series.get('title', series_id)} to '{actual_rule_name}'")
                                break
            
            # Save if any changes made
            if drift_fixed > 0 or orphaned > 0:
                save_config(config)
            
            # Summary
            if drift_fixed > 0 or drift_synced > 0 or orphaned > 0:
                cleanup_logger.info(f"🏷️  Tag reconciliation: {drift_fixed} moved, {drift_synced} synced, {orphaned} orphaned")
            else:
                cleanup_logger.info("🏷️  Tag reconciliation: All tags in sync")
                
        except Exception as e:
            cleanup_logger.error(f"❌ Error in tag reconciliation: {str(e)}")
        # ==================== END PHASE 0 ====================
        
        total_processed = 0
        
        # PRIORITY 1: DORMANT (oldest, most aggressive)
        cleanup_logger.info("🔴 Phase 1: Dormant cleanup (delete ALL episodes from abandoned series)")
        dormant_count = run_dormant_cleanup()
        total_processed += dormant_count
        cleanup_logger.info(f"🔴 Dormant result: {dormant_count} operations")
        
        # Check if storage target met after dormant
        if storage_gated and dormant_count > 0:
            current_disk = get_sonarr_disk_space()
            if current_disk and current_disk['free_space_gb'] >= storage_min_gb:
                cleanup_logger.info(f"🎯 TARGET REACHED after dormant: {current_disk['free_space_gb']:.1f}GB >= {storage_min_gb}GB")
                cleanup_logger.info("✅ Stopping cleanup - goal achieved")
                return total_processed
        
        # PRIORITY 2: GRACE WATCHED (delete watched episodes from inactive series)
        cleanup_logger.info("🟡 Phase 2: Grace watched cleanup (delete watched episodes from inactive series)")
        watched_count = run_grace_watched_cleanup()
        total_processed += watched_count
        cleanup_logger.info(f"🟡 Grace watched result: {watched_count} operations")
        
        # Check if storage target met after grace watched
        if storage_gated and watched_count > 0:
            current_disk = get_sonarr_disk_space()
            if current_disk and current_disk['free_space_gb'] >= storage_min_gb:
                cleanup_logger.info(f"🎯 TARGET REACHED after grace watched: {current_disk['free_space_gb']:.1f}GB >= {storage_min_gb}GB")
                cleanup_logger.info("✅ Stopping cleanup - goal achieved")
                return total_processed
        
        # PRIORITY 3: GRACE UNWATCHED (delete unwatched episodes past deadline)
        cleanup_logger.info("⏰ Phase 3: Grace unwatched cleanup (delete unwatched episodes past deadline)")
        unwatched_count = run_grace_unwatched_cleanup()
        total_processed += unwatched_count
        cleanup_logger.info(f"⏰ Grace unwatched result: {unwatched_count} operations")
        
        # Final status
        final_disk = get_sonarr_disk_space()
        cleanup_logger.info("=" * 80)
        cleanup_logger.info("✅ UNIFIED CLEANUP COMPLETED")
        cleanup_logger.info(f"📊 Total operations: {total_processed}")
        cleanup_logger.info(f"   🔴 Dormant: {dormant_count}")
        cleanup_logger.info(f"   🟡 Grace watched: {watched_count}")
        cleanup_logger.info(f"   ⏰ Grace unwatched: {unwatched_count}")
        
        if final_disk:
            cleanup_logger.info(f"💾 Final free space: {final_disk['free_space_gb']:.1f}GB")
            if storage_gated:
                gate_status = "CLOSED" if final_disk['free_space_gb'] >= storage_min_gb else "STILL OPEN"
                cleanup_logger.info(f"🚪 Storage gate: {gate_status}")
        
        cleanup_logger.info("=" * 80)
        return total_processed
        
    except Exception as e:
        cleanup_logger.error(f"❌ Error in unified cleanup: {str(e)}")
        return 0
# Add these to media_processor.py



def get_jellyfin_session_by_id(session_id):
    """Get a specific Jellyfin session by ID."""
    try:
        jellyfin_url, jellyfin_api_key, _ = get_jellyfin_settings()
        
        if not jellyfin_url or not jellyfin_api_key:
            logger.warning("Jellyfin not configured")
            return None
        
        headers = {'X-Emby-Token': jellyfin_api_key}
        response = requests.get(f"{jellyfin_url}/Sessions", headers=headers, timeout=10)
        
        if response.ok:
            sessions = response.json()
            for session in sessions:
                if session.get('Id') == session_id:
                    return session
        return None
        
    except Exception as e:
        logger.error(f"Error getting Jellyfin session {session_id}: {str(e)}")
        return None

def extract_episode_info_from_session(session):
    """Extract episode information from Jellyfin session."""
    try:
        now_playing = session.get('NowPlayingItem', {})
        play_state = session.get('PlayState', {})
        
        if now_playing.get('Type') != 'Episode':
            return None
        
        position_ticks = play_state.get('PositionTicks', 0)
        total_ticks = now_playing.get('RunTimeTicks', 0)
        
        if total_ticks > 0:
            progress_percent = (position_ticks / total_ticks) * 100
        else:
            progress_percent = 0
        
        return {
            'session_id': session.get('Id'),
            'user_name': session.get('UserName', 'Unknown'),
            'series_name': now_playing.get('SeriesName'),
            'season_number': now_playing.get('ParentIndexNumber'),
            'episode_number': now_playing.get('IndexNumber'),
            'episode_title': now_playing.get('Name', 'Unknown Episode'),
            'progress_percent': progress_percent,
            'is_paused': play_state.get('IsPaused', False)
        }
        
    except Exception as e:
        logger.error(f"Error extracting episode info: {str(e)}")
        return None

def should_trigger_processing(current_progress, trigger_percentage):
    """Check if we've crossed the trigger threshold."""
    return current_progress >= trigger_percentage

def process_jellyfin_episode(episode_info):
    """Process the episode using existing webhook logic."""
    try:
        series_name = episode_info['series_name']
        season_number = episode_info['season_number']
        episode_number = episode_info['episode_number']
        
        # Create episode key for duplicate checking (reuse existing logic)
        episode_key = f"{series_name}|{season_number}|{episode_number}"
        current_time = time.time()
        
        # Use existing duplicate prevention
        with LAST_PROCESSED_LOCK:
            five_minutes_ago = current_time - (5 * 60)
            last_processed_time = LAST_PROCESSED_JELLYFIN_EPISODES.get(episode_key)
            
            if last_processed_time and last_processed_time > five_minutes_ago:
                logger.info(f"⏭️ Skipping duplicate processing for {episode_key} within 5 minutes")
                return False
            else:
                LAST_PROCESSED_JELLYFIN_EPISODES[episode_key] = current_time
        
        logger.info(f"🎯 Processing Jellyfin episode at {episode_info['progress_percent']:.1f}%: {series_name} S{season_number}E{episode_number}")
        
        # Create webhook data format (reuse existing structure)
        jellyfin_data = {
            "server_title": series_name,
            "server_season_num": str(season_number),
            "server_ep_num": str(episode_number)
        }
        
        # Write to temp file (existing pattern)
        temp_dir = os.path.join(os.getcwd(), 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        with open(os.path.join(temp_dir, 'data_from_server.json'), 'w') as f:
            json.dump(jellyfin_data, f)
        
        # Process using existing subprocess call
        result = subprocess.run(
            ["python3", os.path.join(os.getcwd(), "media_processor.py")], 
            capture_output=True, 
            text=True
        )
        
        if result.stderr:
            logger.error(f"Errors from media_processor.py: {result.stderr}")
        
        logger.info(f"✅ Jellyfin polling processing complete for {series_name} S{season_number}E{episode_number}")
        return True
        
    except Exception as e:
        logger.error(f"Error processing Jellyfin episode: {str(e)}")
        return False

def poll_jellyfin_session(session_id, initial_episode_info):
    """Poll a specific Jellyfin session until trigger percentage or session ends."""
    logger.info(f"🔄 Starting Jellyfin polling for session {session_id}")
    logger.info(f"   📺 {initial_episode_info['series_name']} S{initial_episode_info['season_number']}E{initial_episode_info['episode_number']}")
    logger.info(f"   🎯 Will trigger at {JELLYFIN_TRIGGER_PERCENTAGE}% progress")
    
    try:
        processed = False
        poll_count = 0
        
        while session_id in active_polling_sessions and not processed:
            poll_count += 1
            
            # Get current session state
            current_session = get_jellyfin_session_by_id(session_id)
            
            if not current_session:
                logger.info(f"📺 Session {session_id} ended - stopping polling (poll #{poll_count})")
                break
            
            # Extract current episode info
            current_episode_info = extract_episode_info_from_session(current_session)
            
            if not current_episode_info:
                logger.info(f"⏭️ Session {session_id} no longer playing episode - stopping polling")
                break
            
            # Check if we're still on the same episode
            if (current_episode_info['series_name'] != initial_episode_info['series_name'] or
                current_episode_info['season_number'] != initial_episode_info['season_number'] or
                current_episode_info['episode_number'] != initial_episode_info['episode_number']):
                logger.info(f"📺 Episode changed in session {session_id} - stopping polling for original episode")
                break
            
            current_progress = current_episode_info['progress_percent']
            is_paused = current_episode_info['is_paused']
            
            logger.info(f"📊 Poll #{poll_count}: {current_progress:.1f}% {'(PAUSED)' if is_paused else ''}")
            
            # Check if we should trigger processing
            if should_trigger_processing(current_progress, JELLYFIN_TRIGGER_PERCENTAGE):
                logger.info(f"🎯 Trigger threshold reached! Processing at {current_progress:.1f}%")
                
                success = process_jellyfin_episode(current_episode_info)
                if success:
                    processed = True
                    logger.info(f"✅ Successfully processed - stopping polling for session {session_id}")
                else:
                    logger.warning(f"⚠️ Processing failed - continuing polling")
            
            # Wait before next poll (unless we just processed)
            if not processed:
                time.sleep(JELLYFIN_POLL_INTERVAL)
        
        if not processed and session_id not in active_polling_sessions:
            logger.info(f"🔄 Polling stopped for session {session_id} - session ended before trigger")
        
    except Exception as e:
        logger.error(f"❌ Error in Jellyfin polling thread for session {session_id}: {str(e)}")
    
    finally:
        # Clean up
        with polling_lock:
            if session_id in active_polling_sessions:
                del active_polling_sessions[session_id]
            if session_id in polling_threads:
                del polling_threads[session_id]
        
        logger.info(f"🧹 Cleaned up polling for session {session_id}")

def start_jellyfin_polling(session_id, episode_info):
    """Start polling for a specific Jellyfin session."""
    with polling_lock:
        # Don't start if already polling this session
        if session_id in active_polling_sessions:
            logger.info(f"⏭️ Already polling session {session_id} - skipping")
            return False
        
        # Store session info
        active_polling_sessions[session_id] = episode_info
        
        logger.info(f"🎬 Starting Jellyfin polling for: {episode_info['series_name']} S{episode_info['season_number']}E{episode_info['episode_number']}")
        logger.info(f"   👤 User: {episode_info['user_name']}")
        logger.info(f"   🔄 Session ID: {session_id}")
        
        # Start polling thread
        thread = threading.Thread(
            target=poll_jellyfin_session,
            args=(session_id, episode_info),
            daemon=True,
            name=f"JellyfinPoll-{session_id[:8]}"
        )
        thread.start()
        polling_threads[session_id] = thread
        
        return True

def stop_jellyfin_polling(session_id, episode_info=None):
    """Stop polling for a specific session."""
    with polling_lock:
        if session_id in active_polling_sessions:
            logger.info(f"🛑 Stopping Jellyfin polling for session {session_id}")
            del active_polling_sessions[session_id]
            return True
        return False

def get_jellyfin_polling_status():
    """Get current polling status for debugging."""
    with polling_lock:
        active_sessions = list(active_polling_sessions.keys())
        thread_count = len(polling_threads)
        
        return {
            'active_sessions': active_sessions,
            'thread_count': thread_count,
            'trigger_percentage': JELLYFIN_TRIGGER_PERCENTAGE,
            'poll_interval': JELLYFIN_POLL_INTERVAL
        }

# ============================================================
# EMBY SESSION POLLING
# ============================================================
# Mirrors the Jellyfin polling system but queries Emby's /Sessions API.
# Emby's session response has the same shape as Jellyfin's:
#   NowPlayingItem.SeriesName, ParentIndexNumber, IndexNumber, RunTimeTicks
#   PlayState.PositionTicks, IsPaused
# ============================================================

# Separate tracking dicts for Emby so Jellyfin and Emby polls don't collide
active_emby_polling_sessions = {}
emby_polling_threads = {}

def get_emby_session_by_id(session_id):
    """
    Get an active Emby session by Session.Id.
    
    NOTE: Emby's webhook includes PlaySessionId, but the /Sessions API response 
    does NOT populate PlayState.PlaySessionId. We must match on Session.Id instead.
    """
    import requests
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    # Use database-first approach
    emby_url, emby_api_key, _ = get_emby_settings()
    
    print(f"get_emby_session_by_id CALLED for {session_id}")
    print(f"EMBY_URL={emby_url}")
    print(f"EMBY_API_KEY={'SET' if emby_api_key else 'NOT SET'}")
    
    try:
        url = f"{emby_url}/Sessions"
        headers = {'X-Emby-Token': emby_api_key}
        
        print(f"About to GET {url}")
        response = requests.get(url, headers=headers, timeout=10)
        print(f"Response status: {response.status_code}")
        
        if response.status_code != 200:
            print(f"ERROR: API returned {response.status_code}")
            return None
        
        sessions = response.json()
        print(f"Found {len(sessions)} sessions")
        print(f"Looking for Session.Id={session_id}")
        
        # Match on Session.Id (PlayState.PlaySessionId is not populated by Emby API)
        for i, session in enumerate(sessions):
            session_id_api = session.get('Id', 'N/A')
            device_id = session.get('DeviceId', 'N/A')
            
            # Check if this session has active playback
            now_playing = session.get('NowPlayingItem')
            
            if now_playing:
                media_type = now_playing.get('Type', 'None')
                print(f"Session {i}: Id={session_id_api}, DeviceId={device_id}, NowPlaying={media_type}")
                
                # Match on Session.Id
                if session_id_api == session_id:
                    print(f"✅ MATCH FOUND on Session.Id!")
                    return session
            else:
                print(f"Session {i}: Id={session_id_api}, DeviceId={device_id}, NowPlayingItem=None")
        
        print("No match found, returning None")
        return None
        
    except requests.RequestException as e:
        print(f"REQUEST ERROR: {e}")
        return None
    except Exception as e:
        print(f"UNEXPECTED ERROR: {e}")
        return None

def extract_episode_info_from_emby_session(session):
    """Extract episode info from an Emby session. Same structure as Jellyfin."""
    try:
        now_playing = session.get('NowPlayingItem', {})
        play_state = session.get('PlayState', {})

        if now_playing.get('Type') != 'Episode':
            return None

        position_ticks = play_state.get('PositionTicks', 0)
        total_ticks = now_playing.get('RunTimeTicks', 0)
        progress_percent = (position_ticks / total_ticks * 100) if total_ticks > 0 else 0

        return {
            'session_id': session.get('Id'),
            'user_name': session.get('UserName', 'Unknown'),
            'series_name': now_playing.get('SeriesName'),
            'season_number': now_playing.get('ParentIndexNumber'),
            'episode_number': now_playing.get('IndexNumber'),
            'episode_title': now_playing.get('Name', 'Unknown Episode'),
            'progress_percent': progress_percent,
            'is_paused': play_state.get('IsPaused', False),
            'sonarr_id': now_playing.get('ProviderIds', {}).get('sonarr')
        }

    except Exception as e:
        logger.error(f"Error extracting Emby episode info: {str(e)}")
        return None

def poll_emby_session(session_id, initial_episode_info):
    """Poll an Emby session until trigger percentage hit or session ends."""
    # CRITICAL DEBUG: Write to file
    try:
        with open('/app/logs/emby_debug.txt', 'a') as f:
            import datetime
            f.write(f"{datetime.datetime.now()} - poll_emby_session THREAD STARTED - Session: {session_id}\n")
            f.flush()
    except:
        pass
    
    logger.info(f"[EMBY POLL DEBUG] Thread started for session {session_id}")
    logger.info(f"🔄 Starting Emby polling for session {session_id}")
    logger.info(f"   📺 {initial_episode_info['series_name']} S{initial_episode_info['season_number']}E{initial_episode_info['episode_number']}")
    logger.info(f"   🎯 Will trigger at {EMBY_TRIGGER_PERCENTAGE}%")

    try:
        with open('/app/logs/emby_debug.txt', 'a') as f:
            import datetime
            f.write(f"{datetime.datetime.now()} - About to enter try block\n")
            f.flush()
    except:
        pass

    try:
        processed = False
        poll_count = 0
        
        try:
            with open('/app/logs/emby_debug.txt', 'a') as f:
                import datetime
                f.write(f"{datetime.datetime.now()} - About to sleep 5 seconds\n")
                f.flush()
        except:
            pass
        
        # Give Emby a moment to register the session before first poll
        logger.info(f"[EMBY POLL] Waiting 5 seconds before first poll...")
        time.sleep(5)
        
        try:
            with open('/app/logs/emby_debug.txt', 'a') as f:
                import datetime
                f.write(f"{datetime.datetime.now()} - After sleep, about to enter while loop\n")
                f.write(f"{datetime.datetime.now()} - EMBY_POLL_INTERVAL={EMBY_POLL_INTERVAL}\n")
                f.flush()
        except:
            pass
        
        logger.info(f"[EMBY POLL] Entering while loop for session {session_id}")
        logger.info(f"[EMBY POLL] session_id in active_emby_polling_sessions: {session_id in active_emby_polling_sessions}")
        logger.info(f"[EMBY POLL] EMBY_POLL_INTERVAL: {EMBY_POLL_INTERVAL}")
        
        try:
            with open('/app/logs/emby_debug.txt', 'a') as f:
                import datetime
                f.write(f"{datetime.datetime.now()} - Checking while condition\n")
                f.write(f"{datetime.datetime.now()} - session_id={session_id}\n")
                f.write(f"{datetime.datetime.now()} - active_emby_polling_sessions keys: {list(active_emby_polling_sessions.keys())}\n")
                f.write(f"{datetime.datetime.now()} - session in dict: {session_id in active_emby_polling_sessions}\n")
                f.write(f"{datetime.datetime.now()} - processed={processed}\n")
                f.write(f"{datetime.datetime.now()} - While condition would be: {session_id in active_emby_polling_sessions and not processed}\n")
                f.flush()
        except Exception as ex:
            with open('/app/logs/emby_debug.txt', 'a') as f:
                import datetime
                f.write(f"{datetime.datetime.now()} - Error writing debug: {ex}\n")
                f.flush()

        while session_id in active_emby_polling_sessions and not processed:
            try:
                with open('/app/logs/emby_debug.txt', 'a') as f:
                    import datetime
                    f.write(f"{datetime.datetime.now()} - INSIDE while loop iteration - poll_count={poll_count}\n")
                    f.flush()
            except:
                pass
            
            poll_count += 1
            logger.info(f"[EMBY POLL] Loop iteration #{poll_count}")
            
            try:
                with open('/app/logs/emby_debug.txt', 'a') as f:
                    import datetime
                    f.write(f"{datetime.datetime.now()} - About to call get_emby_session_by_id\n")
                    f.flush()
            except:
                pass

            current_session = get_emby_session_by_id(session_id)
            
            try:
                with open('/app/logs/emby_debug.txt', 'a') as f:
                    import datetime
                    f.write(f"{datetime.datetime.now()} - get_emby_session_by_id returned: {current_session is not None}\n")
                    f.flush()
            except:
                pass
            
            logger.info(f"[EMBY POLL] get_emby_session_by_id returned: {current_session is not None}")

            if not current_session:
                if poll_count <= 3:
                    # Session might not be registered yet on first few polls - retry
                    logger.info(f"📺 Emby session {session_id} not found yet (poll #{poll_count}) - will retry")
                    logger.info(f"[EMBY POLL] Session not found (early poll) - retrying")
                    time.sleep(EMBY_POLL_INTERVAL)
                    continue
                else:
                    logger.info(f"📺 Emby session {session_id} ended - stopping polling (poll #{poll_count})")
                    logger.info(f"[EMBY POLL] Session not found after {poll_count} polls - breaking")
                    break

            current_episode_info = extract_episode_info_from_emby_session(current_session)
            
            try:
                with open('/app/logs/emby_debug.txt', 'a') as f:
                    import datetime
                    f.write(f"{datetime.datetime.now()} - extract returned: {current_episode_info is not None}\n")
                    if current_episode_info:
                        f.write(f"{datetime.datetime.now()} - Progress: {current_episode_info.get('progress_percent')}%\n")
                    f.flush()
            except:
                pass

            if not current_episode_info:
                logger.info(f"⏭️ Emby session {session_id} no longer playing episode - stopping polling")
                try:
                    with open('/app/logs/emby_debug.txt', 'a') as f:
                        import datetime
                        f.write(f"{datetime.datetime.now()} - extract returned None, breaking\n")
                        f.flush()
                except:
                    pass
                break

            # Episode changed (autoplay kicked in) — stop polling this one
            if (current_episode_info['series_name'] != initial_episode_info['series_name'] or
                current_episode_info['season_number'] != initial_episode_info['season_number'] or
                current_episode_info['episode_number'] != initial_episode_info['episode_number']):
                logger.info(f"📺 Emby: Episode changed in session {session_id} - processing original if threshold met")
                # Don't just break — the original episode was watched enough if we got here
                # but we don't have its final position. Safe to just stop; the new episode's
                # playback.start will start its own polling.
                break

            current_progress = current_episode_info['progress_percent']
            is_paused = current_episode_info['is_paused']

            logger.info(f"📊 Emby poll #{poll_count}: {current_progress:.1f}% {'(PAUSED)' if is_paused else ''}")

            if should_trigger_processing(current_progress, EMBY_TRIGGER_PERCENTAGE):
                logger.info(f"🎯 Emby trigger threshold reached at {current_progress:.1f}%!")
                
                try:
                    with open('/app/logs/emby_debug.txt', 'a') as f:
                        import datetime
                        f.write(f"{datetime.datetime.now()} - Trigger threshold reached! Processing...\n")
                        f.flush()
                except:
                    pass

                # Reuse process_jellyfin_episode — it just writes temp file + runs media_processor
                success = process_jellyfin_episode(current_episode_info)
                
                try:
                    with open('/app/logs/emby_debug.txt', 'a') as f:
                        import datetime
                        f.write(f"{datetime.datetime.now()} - process_jellyfin_episode returned: {success}\n")
                        f.flush()
                except:
                    pass
                
                if success:
                    processed = True
                    
                    try:
                        with open('/app/logs/emby_debug.txt', 'a') as f:
                            import datetime
                            f.write(f"{datetime.datetime.now()} - processed=True, loop will exit\n")
                            f.flush()
                    except:
                        pass
                    
                    # Mark so playback.stop doesn't double-process
                    tracking_key = get_episode_tracking_key(
                        current_episode_info['series_name'],
                        current_episode_info['season_number'],
                        current_episode_info['episode_number'],
                        current_episode_info['user_name']
                    )
                    processed_jellyfin_episodes.add(tracking_key)
                    logger.info(f"✅ Emby: Processed {current_episode_info['series_name']} S{current_episode_info['season_number']}E{current_episode_info['episode_number']}")
                else:
                    logger.warning(f"⚠️ Emby: Processing failed - will retry next poll")

            if not processed:
                time.sleep(EMBY_POLL_INTERVAL)

    except Exception as e:
        logger.error(f"❌ Error in Emby polling thread for session {session_id}: {str(e)}")
        try:
            with open('/app/logs/emby_debug.txt', 'a') as f:
                import datetime, traceback
                f.write(f"{datetime.datetime.now()} - EXCEPTION in polling thread: {str(e)}\n")
                f.write(f"{traceback.format_exc()}\n")
                f.flush()
        except:
            pass

    finally:
        try:
            with open('/app/logs/emby_debug.txt', 'a') as f:
                import datetime
                f.write(f"{datetime.datetime.now()} - FINALLY block - cleaning up session {session_id}\n")
                f.flush()
        except:
            pass
        
        with polling_lock:
            if session_id in active_emby_polling_sessions:
                del active_emby_polling_sessions[session_id]
            if session_id in emby_polling_threads:
                del emby_polling_threads[session_id]
        logger.info(f"🧹 Cleaned up Emby polling for session {session_id}")

def start_emby_polling(session_id, episode_info):
    """Start polling an Emby session. Called from episeerr.py on playback.start."""
    # CRITICAL DEBUG: Write to file to bypass logging
    try:
        with open('/app/logs/emby_debug.txt', 'a') as f:
            import datetime
            f.write(f"{datetime.datetime.now()} - start_emby_polling CALLED - Session: {session_id}\n")
            f.flush()
    except:
        pass
    
    # CRITICAL DEBUG: This should ALWAYS appear if the function is called
    import logging
    root_logger = logging.getLogger()
    root_logger.info(f"!!! EMBY POLLING FUNCTION CALLED - Session: {session_id} !!!")
    
    logger.info(f"[EMBY DEBUG] start_emby_polling called for session {session_id}")
    
    # Check if this episode was already processed via polling
    tracking_key = get_episode_tracking_key(
        episode_info['series_name'],
        episode_info['season_number'],
        episode_info['episode_number'],
        episode_info['user_name']
    )
    
    if tracking_key in processed_jellyfin_episodes:
        logger.info(f"⏭️ Emby: {episode_info['series_name']} S{episode_info['season_number']}E{episode_info['episode_number']} already processed - skipping polling")
        try:
            with open('/app/logs/emby_debug.txt', 'a') as f:
                import datetime
                f.write(f"{datetime.datetime.now()} - Episode already processed, skipping\n")
                f.flush()
        except:
            pass
        return False
    
    with polling_lock:
        if session_id in active_emby_polling_sessions:
            logger.info(f"⏭️ Already polling Emby session {session_id} - skipping")
            logger.info(f"[EMBY DEBUG] Already polling - skipping")
            return False

        active_emby_polling_sessions[session_id] = episode_info
        
        try:
            with open('/app/logs/emby_debug.txt', 'a') as f:
                import datetime
                f.write(f"{datetime.datetime.now()} - Added session to active dict\n")
                f.write(f"{datetime.datetime.now()} - session_id={session_id}\n")
                f.write(f"{datetime.datetime.now()} - active_emby_polling_sessions keys: {list(active_emby_polling_sessions.keys())}\n")
                f.flush()
        except:
            pass

        logger.info(f"🎬 Starting Emby polling: {episode_info['series_name']} S{episode_info['season_number']}E{episode_info['episode_number']}")
        logger.info(f"   👤 User: {episode_info['user_name']}")
        logger.info(f"   🔄 Session ID: {session_id}")
        logger.info(f"[EMBY DEBUG] About to spawn thread for {session_id}")

        thread = threading.Thread(
            target=poll_emby_session,
            args=(session_id, episode_info),
            daemon=True,
            name=f"EmbyPoll-{session_id[:8]}"
        )
        thread.start()
        emby_polling_threads[session_id] = thread
        logger.info(f"[EMBY DEBUG] Thread started: {thread.name}, alive={thread.is_alive()}")

        return True

def stop_emby_polling(session_id):
    """Stop polling an Emby session. Called from episeerr.py on playback.stop."""
    try:
        with open('/app/logs/emby_debug.txt', 'a') as f:
            import datetime
            f.write(f"{datetime.datetime.now()} - stop_emby_polling CALLED - Session: {session_id}\n")
            f.flush()
    except:
        pass
    
    with polling_lock:
        if session_id in active_emby_polling_sessions:
            logger.info(f"🛑 Stopping Emby polling for session {session_id}")
            try:
                with open('/app/logs/emby_debug.txt', 'a') as f:
                    import datetime
                    f.write(f"{datetime.datetime.now()} - REMOVING session from active dict: {session_id}\n")
                    f.flush()
            except:
                pass
            del active_emby_polling_sessions[session_id]
            return True
        return False

# Add this function to handle webhook start events
def handle_jellyfin_playback_start(webhook_data):
    """Handle Jellyfin playback start webhook and initiate polling."""
    try:
        # Extract session info from webhook
        session_id = webhook_data.get('SessionId')  # Check your webhook payload for correct field
        series_name = webhook_data.get('SeriesName')
        season_number = webhook_data.get('SeasonNumber')
        episode_number = webhook_data.get('EpisodeNumber')
        user_name = webhook_data.get('UserName', 'Unknown')
        
        # Validate required fields
        if not all([session_id, series_name, season_number is not None, episode_number is not None]):
            logger.warning(f"Missing required fields in Jellyfin start webhook: {webhook_data}")
            return False
        
        # Create episode info for polling
        episode_info = {
            'session_id': session_id,
            'user_name': user_name,
            'series_name': series_name,
            'season_number': int(season_number),
            'episode_number': int(episode_number),
            'progress_percent': 0.0,
            'is_paused': False
        }
        
        # Start polling
        return start_jellyfin_polling(session_id, episode_info)
        
    except Exception as e:
        logger.error(f"Error handling Jellyfin playback start: {str(e)}")
        return False
    

def get_jellyfin_active_episodes():
    """Get all currently active episodes from Jellyfin sessions."""
    try:
        jellyfin_url, jellyfin_api_key, _ = get_jellyfin_settings()
        
        if not jellyfin_url or not jellyfin_api_key:
            logger.debug("Jellyfin not configured")
            return []
        
        headers = {'X-Emby-Token': jellyfin_api_key}
        response = requests.get(f"{jellyfin_url}/Sessions", headers=headers, timeout=10)
        
        if not response.ok:
            logger.warning(f"Failed to get Jellyfin sessions: {response.status_code}")
            return []
        
        sessions = response.json()
        active_episodes = []
        
        for session in sessions:
            now_playing = session.get('NowPlayingItem', {})
            play_state = session.get('PlayState', {})
            
            # Only process episodes
            if now_playing.get('Type') != 'Episode':
                continue
            
            # Skip if paused (optional - you might want to process paused episodes too)
            if play_state.get('IsPaused', False):
                logger.debug(f"Skipping paused episode: {now_playing.get('SeriesName')} S{now_playing.get('ParentIndexNumber')}E{now_playing.get('IndexNumber')}")
                continue
            
            # Calculate progress
            position_ticks = play_state.get('PositionTicks', 0)
            total_ticks = now_playing.get('RunTimeTicks', 0)
            
            if total_ticks > 0:
                progress_percent = (position_ticks / total_ticks) * 100
            else:
                progress_percent = 0
            
            # Create episode info
            episode_info = {
                'user_name': session.get('UserName', 'Unknown'),
                'series_name': now_playing.get('SeriesName', 'Unknown'),
                'season_number': now_playing.get('ParentIndexNumber', 0),
                'episode_number': now_playing.get('IndexNumber', 0),
                'episode_title': now_playing.get('Name', 'Unknown'),
                'progress_percent': progress_percent,
                'device_name': session.get('DeviceName', 'Unknown'),
                'session_id': session.get('Id', 'Unknown')
            }
            
            # Create unique episode key for tracking
            episode_key = f"{episode_info['user_name']}|{episode_info['series_name']}|{episode_info['season_number']}|{episode_info['episode_number']}"
            episode_info['episode_key'] = episode_key
            
            active_episodes.append(episode_info)
        
        return active_episodes
        
    except Exception as e:
        logger.error(f"Error getting Jellyfin active episodes: {str(e)}")
        return []

def should_process_episode(episode_info):
    """Determine if we should process this episode."""
    episode_key = episode_info['episode_key']
    progress = episode_info['progress_percent']
    
    # Check if we've already processed this episode
    if episode_key in processed_episodes:
        last_processed_time = processed_episodes[episode_key]['timestamp']
        last_processed_progress = processed_episodes[episode_key]['progress']
        
        # Don't process again if we processed it recently (within 4 hours)
        four_hours_ago = time.time() - (4 * 60 * 60)
        if last_processed_time > four_hours_ago:
            logger.debug(f"Already processed {episode_key} recently at {last_processed_progress:.1f}%")
            return False
    
    # Process if progress is above threshold
    if progress >= JELLYFIN_TRIGGER_PERCENTAGE:
        logger.info(f"🎯 Episode ready for processing: {episode_info['series_name']} S{episode_info['season_number']}E{episode_info['episode_number']}")
        logger.info(f"   📊 Progress: {progress:.1f}% >= {JELLYFIN_TRIGGER_PERCENTAGE}%")
        logger.info(f"   👤 User: {episode_info['user_name']} ({episode_info['device_name']})")
        return True
    
    logger.debug(f"Episode below threshold: {episode_key} at {progress:.1f}%")
    return False

def process_jellyfin_episode_active_polling(episode_info):
    """Process the episode using existing webhook logic."""
    try:
        series_name = episode_info['series_name']
        season_number = episode_info['season_number']
        episode_number = episode_info['episode_number']
        episode_key = episode_info['episode_key']
        
        # Use existing duplicate prevention logic
        jellyfin_episode_key = f"{series_name}|{season_number}|{episode_number}"
        current_time = time.time()
        
        with LAST_PROCESSED_LOCK:
            five_minutes_ago = current_time - (5 * 60)
            last_processed_time = LAST_PROCESSED_JELLYFIN_EPISODES.get(jellyfin_episode_key)
            
            if last_processed_time and last_processed_time > five_minutes_ago:
                logger.info(f"⏭️ Skipping duplicate processing for {jellyfin_episode_key} within 5 minutes")
                return False
            else:
                LAST_PROCESSED_JELLYFIN_EPISODES[jellyfin_episode_key] = current_time
        
        logger.info(f"🎯 Processing Jellyfin episode: {series_name} S{season_number}E{episode_number}")
        logger.info(f"   📊 Progress: {episode_info['progress_percent']:.1f}%")
        logger.info(f"   👤 User: {episode_info['user_name']}")
        
        # Create webhook data format (reuse existing structure)
        jellyfin_data = {
            "server_title": series_name,
            "server_season_num": str(season_number),
            "server_ep_num": str(episode_number)
        }
        
        # Write to temp file (existing pattern)
        temp_dir = os.path.join(os.getcwd(), 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        with open(os.path.join(temp_dir, 'data_from_server.json'), 'w') as f:
            json.dump(jellyfin_data, f)
        
        # Process using existing subprocess call
        result = subprocess.run(
            ["python3", os.path.join(os.getcwd(), "media_processor.py")], 
            capture_output=True, 
            text=True
        )
        
        if result.stderr:
            logger.error(f"Errors from media_processor.py: {result.stderr}")
        
        # Mark as processed
        processed_episodes[episode_key] = {
            'timestamp': current_time,
            'progress': episode_info['progress_percent'],
            'series': series_name,
            'season': season_number,
            'episode': episode_number
        }
        
        logger.info(f"✅ Jellyfin processing complete for {series_name} S{season_number}E{episode_number}")
        return True
        
    except Exception as e:
        logger.error(f"Error processing Jellyfin episode: {str(e)}")
        return False

def cleanup_old_processed_episodes():
    """Clean up old processed episode records."""
    try:
        current_time = time.time()
        twentyfour_hours_ago = current_time - (24 * 60 * 60)
        
        old_episodes = [
            key for key, data in processed_episodes.items() 
            if data['timestamp'] < twentyfour_hours_ago
        ]
        
        for episode_key in old_episodes:
            del processed_episodes[episode_key]
        
        if old_episodes:
            logger.info(f"🧹 Cleaned up {len(old_episodes)} old processed episode records")
            
    except Exception as e:
        logger.error(f"Error cleaning up processed episodes: {str(e)}")

def jellyfin_polling_loop():
    """Main polling loop - runs every 15 minutes."""
    global jellyfin_polling_running
    
    logger.info(f"🔄 Jellyfin active polling started (every {JELLYFIN_POLL_INTERVAL//60} minutes)")
    logger.info(f"🎯 Will process episodes at {JELLYFIN_TRIGGER_PERCENTAGE}% progress")
    
    while jellyfin_polling_running:
        try:
            logger.info("🔍 Checking Jellyfin for active episodes...")
            
            # Get all active episodes
            active_episodes = get_jellyfin_active_episodes()
            
            if not active_episodes:
                logger.info("📺 No active episodes found")
            else:
                logger.info(f"📺 Found {len(active_episodes)} active episodes")
                
                # Process each episode that meets criteria
                processed_count = 0
                for episode_info in active_episodes:
                    if should_process_episode(episode_info):
                        success = process_jellyfin_episode_active_polling(episode_info)
                        if success:
                            processed_count += 1
                
                if processed_count > 0:
                    logger.info(f"✅ Processed {processed_count} episodes this cycle")
                else:
                    logger.info("⏭️ No episodes ready for processing this cycle")
            
            # Clean up old records periodically
            cleanup_old_processed_episodes()
            
            # Wait for next cycle
            logger.info(f"⏰ Next check in {JELLYFIN_POLL_INTERVAL//60} minutes")
            time.sleep(JELLYFIN_POLL_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error in Jellyfin polling loop: {str(e)}")
            time.sleep(300)  # Wait 5 minutes on error
    
    logger.info("🛑 Jellyfin polling stopped")

def start_jellyfin_active_polling():
    """Start the active Jellyfin polling system - auto-detects mode from env vars."""
    global jellyfin_polling_thread, jellyfin_polling_running
    
    # Check if Jellyfin is configured
    jellyfin_url, jellyfin_api_key, _ = get_jellyfin_settings()
    
    if not jellyfin_url or not jellyfin_api_key:
        logger.info("⏭️  Jellyfin not configured - active polling disabled")
        return False
    
    # Auto-detect which Jellyfin mode is configured from database/env
    jellyfin_config = load_jellyfin_config()
    using_trigger_window = jellyfin_config['trigger_min'] and jellyfin_config['trigger_max']
    using_poll_interval = jellyfin_config['poll_interval']
    
    # Option 1: Real-time with PlaybackProgress webhooks
    # Has TRIGGER_MIN/MAX but NO POLL_INTERVAL
    if using_trigger_window and not using_poll_interval:
        logger.info("⏭️  Jellyfin Option 1 detected (TRIGGER_MIN/MAX set)")
        logger.info("⏭️  Using PlaybackProgress webhooks - active polling not needed")
        return False
    
    # Option 3: On-Stop only
    # Has neither TRIGGER_MIN/MAX nor POLL_INTERVAL
    if not using_trigger_window and not using_poll_interval:
        logger.info("⏭️  Jellyfin Option 3 detected (Playback Stop only)")
        logger.info("⏭️  No polling interval configured - active polling disabled")
        return False
    
    # Option 2: Polling mode
    # Has POLL_INTERVAL set (may or may not have TRIGGER_PERCENTAGE)
    if using_poll_interval:
        if jellyfin_polling_running:
            logger.info("⏭️  Jellyfin active polling already running")
            return True
        
        try:
            interval_minutes = int(using_poll_interval) // 60
            logger.info(f"✅ Jellyfin Option 2 detected (POLL_INTERVAL={using_poll_interval})")
            logger.info(f"✅ Active polling enabled: checking every {interval_minutes} minutes")
        except:
            logger.info(f"✅ Jellyfin Option 2 detected (POLL_INTERVAL={using_poll_interval})")
        
        jellyfin_polling_running = True
        jellyfin_polling_thread = threading.Thread(
            target=jellyfin_polling_loop, 
            daemon=True, 
            name="JellyfinActivePolling"
        )
        jellyfin_polling_thread.start()
        
        logger.info("✅ Jellyfin active polling system started")
        return True
    
    # Fallback: shouldn't reach here, but disable polling to be safe
    logger.warning("⚠️  Jellyfin configuration unclear - disabling active polling")
    return False

def stop_jellyfin_active_polling():
    """Stop the active Jellyfin polling system."""
    global jellyfin_polling_running
    
    if not jellyfin_polling_running:
        return False
    
    jellyfin_polling_running = False
    logger.info("🛑 Stopping Jellyfin active polling...")
    
    return True

def get_jellyfin_active_polling_status():
    """Get current polling status for debugging."""
    return {
        'polling_running': jellyfin_polling_running,
        'trigger_percentage': JELLYFIN_TRIGGER_PERCENTAGE,
        'poll_interval_minutes': JELLYFIN_POLL_INTERVAL // 60,
        'processed_episodes_count': len(processed_episodes),
        'processed_episodes': [
            {
                'episode': f"{data['series']} S{data['season']}E{data['episode']}",
                'progress': f"{data['progress']:.1f}%",
                'processed_at': datetime.fromtimestamp(data['timestamp']).strftime("%Y-%m-%d %H:%M:%S")
            }
            for data in processed_episodes.values()
        ]
    }

def main():
    """Main entry point - FIXED webhook vs cleanup logic"""
    # Check if this is a webhook call (has recent webhook data)
    series_name, season_number, episode_number, thetvdb_id, themoviedb_id = get_server_activity()
    
    # ONLY process as webhook if this was called BY a webhook (not manual cleanup)
    # Add a flag or check timestamp to distinguish
    webhook_file = '/app/temp/data_from_server.json'
    
    try:
        # Check if webhook file is recent (within last few minutes)
        if os.path.exists(webhook_file):
            file_age = time.time() - os.path.getmtime(webhook_file)
            is_recent_webhook = file_age < 300  # 5 minutes
        else:
            is_recent_webhook = False
    except:
        is_recent_webhook = False
    
    if series_name and is_recent_webhook:
        # Webhook mode - process the episode that was just watched
        series_id = get_series_id(series_name, thetvdb_id, themoviedb_id)
        if series_id:
            # NEW: Handle tag validation and drift correction before processing
            config = load_config()
            
            # Find current rule in config
            config_rule = None
            for rule_name, rule_details in config['rules'].items():
                if str(series_id) in rule_details.get('series', {}):
                    config_rule = rule_name
                    break
            
            if config_rule:
                matches, actual_tag_rule = validate_series_tag(series_id, config_rule)
                
                if not matches:
                    if actual_tag_rule:
                        # Drift: move config to match tag
                        logger.warning(f"DRIFT DETECTED: config={config_rule}, tag={actual_tag_rule}")
                        if move_series_in_config(series_id, config_rule, actual_tag_rule):
                            # Find actual rule name in config (case-insensitive)
                            # move_series_in_config already handled the case, but we need to update config_rule
                            for rn in config['rules'].keys():
                                if rn.lower() == actual_tag_rule.lower():
                                    config_rule = rn
                                    logger.info(f"Updated config_rule to '{config_rule}' (matched case from config)")
                                    break
                    else:
                        # No tag: sync from config
                        logger.warning(f"No episeerr tag found - syncing to {config_rule}")
                        sync_rule_tag_to_sonarr(series_id, config_rule)
                
                # Now process with (possibly updated) rule
                rule = config['rules'][config_rule]
                process_episodes_for_webhook(series_id, season_number, episode_number, rule, series_name)
            else:
                update_activity_date(series_id, season_number, episode_number)
            return True
    else:
        # Cleanup mode - run unified cleanup (manual or scheduled)
        run_unified_cleanup()
        return False

if __name__ == "__main__":
    main()
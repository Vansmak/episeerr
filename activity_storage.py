"""
Activity Storage Module - WITH BACKDROP SUPPORT + REQUEST SAVING
Logs watch events, search events, and requests with backdrop images
"""

import json
import os
import time
import logging
import requests

from logging_config import main_logger as logger

ACTIVITY_DIR = '/app/data/activity'
os.makedirs(ACTIVITY_DIR, exist_ok=True)

SEARCHES_FILE = os.path.join(ACTIVITY_DIR, 'searches.json')
WATCHES_FILE = os.path.join(ACTIVITY_DIR, 'watched.json')
REQUESTS_FILE = os.path.join(ACTIVITY_DIR, 'last_request.json')

# Sonarr API settings (will be loaded from config)
SONARR_URL = None
SONARR_API_KEY = None

def init_sonarr_config(url, api_key):
    """Initialize Sonarr config for backdrop fetching"""
    global SONARR_URL, SONARR_API_KEY
    SONARR_URL = url
    SONARR_API_KEY = api_key
    logger.info("✅ Activity storage initialized with Sonarr config")

def get_series_backdrop(series_id):
    """Fetch backdrop path from Sonarr series data"""
    try:
        if not SONARR_URL or not SONARR_API_KEY:
            logger.debug("Sonarr config not initialized, skipping backdrop fetch")
            return None
            
        url = f"{SONARR_URL}/api/v3/series/{series_id}"
        headers = {'X-Api-Key': SONARR_API_KEY}
        response = requests.get(url, headers=headers, timeout=5)
        
        if response.ok:
            series_data = response.json()
            # Get fanart/backdrop image
            for image in series_data.get('images', []):
                if image.get('coverType') in ['fanart', 'banner']:
                    backdrop_url = image.get('remoteUrl')
                    logger.debug(f"Found backdrop for series {series_id}: {backdrop_url}")
                    return backdrop_url
            logger.debug(f"No fanart/banner found for series {series_id}")
        else:
            logger.debug(f"Failed to get series {series_id}: {response.status_code}")
        return None
    except Exception as e:
        logger.debug(f"Could not get backdrop for series {series_id}: {e}")
        return None

def save_search_event(series_id, series_title, season, episode, episode_ids):
    """Save when Sonarr searches for episodes"""
    # Get backdrop instead of poster
    backdrop_url = get_series_backdrop(series_id)
    
    event = {
        'series_id': series_id,
        'series_title': series_title,
        'season': season,
        'episode': episode,
        'episode_ids': episode_ids,
        'backdrop_url': backdrop_url,
        'timestamp': int(time.time())
    }
    _append_to_activity_log(SEARCHES_FILE, event, max_entries=10)
    logger.info(f"📝 Logged search event: {series_title} S{season}E{episode}")

def save_watch_event(series_id, series_title, season, episode, user):
    """Save when user watches an episode (with 7-day auto-cleanup)"""
    from datetime import datetime
    
    # Get backdrop instead of poster
    backdrop_url = get_series_backdrop(series_id)
    
    event = {
        'series_id': series_id,
        'series_title': series_title,
        'season': season,
        'episode': episode,
        'user': user,
        'backdrop_url': backdrop_url,
        'timestamp': int(time.time())
    }
    
    # Modified append with 7-day cleanup instead of max_entries
    _append_to_activity_log_with_cleanup(WATCHES_FILE, event, days=7)
    logger.info(f"📝 Logged watch event: {series_title} S{season}E{episode} by {user}")
def save_request_event(request_data):
    """Save Jellyseerr request before file is deleted"""
    try:
        # Extract series ID to get backdrop
        series_id = request_data.get('series_id') or request_data.get('tvdb_id')
        backdrop_url = None
        
        if series_id:
            backdrop_url = get_series_backdrop(series_id)
        
        # Add backdrop to request data
        request_data['backdrop_url'] = backdrop_url
        
        # Save as last_request.json
        with open(REQUESTS_FILE, 'w') as f:
            json.dump(request_data, f, indent=2)
            
        logger.info(f"📝 Logged request: {request_data.get('title', 'Unknown')}")
        
    except Exception as e:
        logger.error(f"Failed to save request event: {e}")
        
def save_search_event(series_id, series_title, season, episode, episode_ids):
    """Save when Sonarr searches for episodes (with 7-day auto-cleanup)"""
    from datetime import datetime
    
    # Get backdrop instead of poster
    backdrop_url = get_series_backdrop(series_id)
    
    event = {
        'series_id': series_id,
        'series_title': series_title,
        'season': season,
        'episode': episode,
        'episode_ids': episode_ids,
        'backdrop_url': backdrop_url,
        'timestamp': int(time.time())
    }
    
    # Modified append with 7-day cleanup instead of max_entries
    _append_to_activity_log_with_cleanup(SEARCHES_FILE, event, days=7)
    logger.info(f"📝 Logged search event: {series_title} S{season}E{episode}")

# ADD THIS NEW HELPER FUNCTION (after the existing _append_to_activity_log)

def _append_to_activity_log_with_cleanup(filepath, event, days=7):
    """Helper to append event and auto-cleanup entries older than N days"""
    try:
        from datetime import datetime
        
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                events = json.load(f)
        else:
            events = []
        
        # Add new event
        events.append(event)
        
        # Auto-cleanup: keep only last N days
        cutoff = datetime.now().timestamp() - (days * 24 * 60 * 60)
        events = [e for e in events if e.get('timestamp', 0) > cutoff]
        
        with open(filepath, 'w') as f:
            json.dump(events, f, indent=2)
            
    except Exception as e:
        logger.error(f"Failed to save activity: {e}")




def get_last_search():
    """Get most recent search event"""
    return _get_last_event(SEARCHES_FILE)

def get_last_watch():
    """Get most recent watch event"""
    return _get_last_event(WATCHES_FILE)

def get_last_request():
    """Get most recent Overseerr request"""
    try:
        if not os.path.exists(REQUESTS_FILE):
            return None
        
        with open(REQUESTS_FILE, 'r') as f:
            return json.load(f)
            
    except Exception as e:
        logger.error(f"Failed to get last request: {e}")
        return None

def _get_last_event(filepath):
    """Helper to get most recent event from log"""
    try:
        if not os.path.exists(filepath):
            return None
        
        with open(filepath, 'r') as f:
            events = json.load(f)
        
        return events[0] if events else None
        
    except Exception as e:
        logger.error(f"Failed to read activity log: {e}")
        return None
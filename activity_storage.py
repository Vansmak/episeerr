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
def save_added_event(title, service, media_type='tv', requested_seasons=None, series_id=None):
    """Save the most recent add event, regardless of source (Jellyseerr, Sonarr, Radarr,
    Xadarr, direct add, etc.) — anything that results in a series/movie webhook firing."""
    try:
        backdrop_url = get_series_backdrop(series_id) if series_id else None

        event = {
            'title': title,
            'service': service,
            'media_type': media_type,
            'requested_seasons': requested_seasons,
            'backdrop_url': backdrop_url,
            'timestamp': int(time.time())
        }

        with open(REQUESTS_FILE, 'w') as f:
            json.dump(event, f, indent=2)

        logger.info(f"📝 Logged added event: {title} via {service}")

    except Exception as e:
        logger.error(f"Failed to save added event: {e}")

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


def _format_time_ago(timestamp):
    """Plain server-side relative time, e.g. '2h ago' - no JS needed for a static page render."""
    seconds = max(0, int(time.time()) - int(timestamp or 0))
    if seconds < 60:
        return 'just now'
    if seconds < 3600:
        return f'{seconds // 60}m ago'
    if seconds < 86400:
        return f'{seconds // 3600}h ago'
    return f'{seconds // 86400}d ago'


def get_last_search_per_rule(config):
    """
    Most recent Get/search action for each rule, keyed by rule_name
    (episeerr#115 follow-up - Rules page as the living showcase of what
    Episeerr actually does, rather than a dry management table).

    Reuses the same series_id -> rule_name join dashboard.py's activity
    feed already does, just grouped by rule instead of flattened into one
    timeline. Returns {} on any error - this is cosmetic, never worth
    failing the Rules page over.
    """
    try:
        series_rule = {}
        for rule_name, rule_data in (config or {}).get('rules', {}).items():
            for sid in (rule_data.get('series') or {}).keys():
                try:
                    series_rule[int(sid)] = rule_name
                except (TypeError, ValueError):
                    continue

        if not os.path.exists(SEARCHES_FILE):
            return {}

        with open(SEARCHES_FILE, 'r') as f:
            events = json.load(f)

        last_action = {}
        for ev in events:
            rule_name = series_rule.get(ev.get('series_id'))
            if not rule_name:
                continue
            existing = last_action.get(rule_name)
            if existing is None or ev.get('timestamp', 0) > existing.get('timestamp', 0):
                last_action[rule_name] = ev

        for ev in last_action.values():
            ev['time_ago'] = _format_time_ago(ev.get('timestamp'))

        return last_action
    except Exception as e:
        logger.error(f"Failed to compute per-rule activity: {e}")
        return {}
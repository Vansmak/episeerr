# modified_episeerr.py
import os
import json
import time
from datetime import datetime
import requests
import logging
import threading
import re
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from logging_config import main_logger as logger
# Load environment variables
load_dotenv()

# ============================================================
# DATABASE SUPPORT - Get settings from DB first, fallback to .env
# ============================================================
from settings_db import get_sonarr_config, get_service

def normalize_url(url):
    """Remove leading/trailing whitespaces and trailing / from URLs"""
    if url is None:
        return None
    normalized_url = url.strip().rstrip('/')
    return normalized_url

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

# In modified_episeerr.py
REQUESTS_DIR = os.path.join(os.getcwd(), 'requests')



# Sonarr connection details - DB first, fallback to .env
SONARR_URL, SONARR_API_KEY = get_sonarr_settings()

# episeerr_utils.py
EPISEERR_DEFAULT_TAG_ID = None
EPISEERR_SELECT_TAG_ID = None

# Directory to store pending requests
REQUESTS_DIR = os.path.join(os.getcwd(), 'data', 'requests')
os.makedirs(REQUESTS_DIR, exist_ok=True)
# Store pending episode selections
# Format: {series_id: {'title': 'Series Title', 'season': 1, 'episodes': [1, 2, 3, ...]}}
pending_selections = {}
# Optional tag creation - defaults to False for safety
AUTO_CREATE_TAGS = os.getenv('EPISEERR_AUTO_CREATE_TAGS', 'false').lower() == 'true'

# Log the setting at startup
if AUTO_CREATE_TAGS:
    logger.info("Auto-create tags ENABLED - will create episeerr_default and episeerr_select tags")
else:
    logger.info("Auto-create tags DISABLED - please create tags manually if using tag-based workflows")



def get_sonarr_headers():
    """Get headers for Sonarr API requests."""
    return {
        'X-Api-Key': SONARR_API_KEY,
        'Content-Type': 'application/json'
    }

def create_episeerr_default_tag():
    """Create a single 'episeerr_default' tag in Sonarr and return its ID."""
    global EPISEERR_DEFAULT_TAG_ID
    
    if not AUTO_CREATE_TAGS:
        logger.debug("Tag auto-creation disabled, checking for existing episeerr_default tag")
        # Still check if tag exists, just don't create it
        try:
            headers = get_sonarr_headers()
            tags_response = requests.get(f"{SONARR_URL}/api/v3/tag", headers=headers, timeout=10)
            
            if tags_response.ok:
                for tag in tags_response.json():
                    if tag['label'].lower() == 'episeerr_default':
                        EPISEERR_DEFAULT_TAG_ID = tag['id']
                        logger.info(f"Found existing 'episeerr_default' tag with ID {EPISEERR_DEFAULT_TAG_ID}")
                        return EPISEERR_DEFAULT_TAG_ID
            
            logger.warning("episeerr_default tag not found. Please create manually in Sonarr or set EPISEERR_AUTO_CREATE_TAGS=true")
            return None
            
        except Exception as e:
            logger.warning(f"Could not check for existing tags: {str(e)}")
            return None
    
    # Original auto-creation logic (when AUTO_CREATE_TAGS=true)
    if EPISEERR_DEFAULT_TAG_ID is not None:
        logger.debug(f"'episeerr_default' tag ID already set: {EPISEERR_DEFAULT_TAG_ID}")
        return EPISEERR_DEFAULT_TAG_ID
    
    try:
        headers = get_sonarr_headers()
        logger.debug(f"Making GET request to {SONARR_URL}/api/v3/tag to fetch existing tags")
       
        tags_response = requests.get(f"{SONARR_URL}/api/v3/tag", headers=headers, timeout=10)
       
        if not tags_response.ok:
            logger.error(f"Failed to get tags. Status: {tags_response.status_code}, Response: {tags_response.text}")
            return None
            
        for tag in tags_response.json():
            if tag['label'].lower() == 'episeerr_default':
                EPISEERR_DEFAULT_TAG_ID = tag['id']
                logger.info(f"Found existing 'episeerr_default' tag with ID {EPISEERR_DEFAULT_TAG_ID}")
                return EPISEERR_DEFAULT_TAG_ID
       
        logger.debug("No 'episeerr_default' tag found, creating new tag")
        tag_create_response = requests.post(
            f"{SONARR_URL}/api/v3/tag",
            headers=headers,
            json={"label": "episeerr_default"},
            timeout=10
        )
        if tag_create_response.ok:
            EPISEERR_DEFAULT_TAG_ID = tag_create_response.json().get('id')
            logger.info(f"Created tag: 'episeerr_default' with ID {EPISEERR_DEFAULT_TAG_ID}")
            return EPISEERR_DEFAULT_TAG_ID
        else:
            logger.error(f"Failed to create episeerr_default tag. Status: {tag_create_response.status_code}, Response: {tag_create_response.text}")
            return None
       
    except requests.Timeout:
        logger.error("Request to Sonarr timed out while creating 'episeerr_default' tag")
        return None
    except Exception as e:
        logger.error(f"Error creating 'episeerr_default' tag: {str(e)}")
        return None

def create_episeerr_select_tag():
    """Create a single 'episeerr_select' tag in Sonarr and return its ID."""
    global EPISEERR_SELECT_TAG_ID
    
    if not AUTO_CREATE_TAGS:
        logger.debug("Tag auto-creation disabled, checking for existing episeerr_select tag")
        # Still check if tag exists, just don't create it
        try:
            headers = get_sonarr_headers()
            tags_response = requests.get(f"{SONARR_URL}/api/v3/tag", headers=headers, timeout=10)
            
            if tags_response.ok:
                for tag in tags_response.json():
                    if tag['label'].lower() == 'episeerr_select':
                        EPISEERR_SELECT_TAG_ID = tag['id']
                        logger.info(f"Found existing 'episeerr_select' tag with ID {EPISEERR_SELECT_TAG_ID}")
                        return EPISEERR_SELECT_TAG_ID
            
            logger.warning("episeerr_select tag not found. Please create manually in Sonarr or set EPISEERR_AUTO_CREATE_TAGS=true")
            return None
            
        except Exception as e:
            logger.warning(f"Could not check for existing tags: {str(e)}")
            return None
    
    # Original auto-creation logic (when AUTO_CREATE_TAGS=true)  
    if EPISEERR_SELECT_TAG_ID is not None:
        logger.debug(f"'episeerr_select' tag ID already set: {EPISEERR_SELECT_TAG_ID}")
        return EPISEERR_SELECT_TAG_ID
    
    try:
        headers = get_sonarr_headers()
        logger.debug(f"Making GET request to {SONARR_URL}/api/v3/tag to fetch existing tags")
       
        tags_response = requests.get(f"{SONARR_URL}/api/v3/tag", headers=headers, timeout=10)
       
        if not tags_response.ok:
            logger.error(f"Failed to get tags. Status: {tags_response.status_code}, Response: {tags_response.text}")
            return None
            
        for tag in tags_response.json():
            if tag['label'].lower() == 'episeerr_select':
                EPISEERR_SELECT_TAG_ID = tag['id']
                logger.info(f"Found existing 'episeerr_select' tag with ID {EPISEERR_SELECT_TAG_ID}")
                return EPISEERR_SELECT_TAG_ID
       
        logger.debug("No 'episeerr_select' tag found, creating new tag")
        tag_create_response = requests.post(
            f"{SONARR_URL}/api/v3/tag",
            headers=headers,
            json={"label": "episeerr_select"},
            timeout=10
        )
        if tag_create_response.ok:
            EPISEERR_SELECT_TAG_ID = tag_create_response.json().get('id')
            logger.info(f"Created tag: 'episeerr_select' with ID {EPISEERR_SELECT_TAG_ID}")
            return EPISEERR_SELECT_TAG_ID
        else:
            logger.error(f"Failed to create episeerr_select tag. Status: {tag_create_response.status_code}, Response: {tag_create_response.text}")
            return None
       
    except requests.Timeout:
        logger.error("Request to Sonarr timed out while creating 'episeerr_select' tag")
        return None
    except Exception as e:
        logger.error(f"Error creating 'episeerr_select' tag: {str(e)}")
        return None
def get_or_create_rule_tag_id(rule_name):
    """
    Create episeerr_<rulename> tag in Sonarr if doesn't exist.
    Returns tag ID.
    
    Args:
        rule_name: Name of the rule (e.g., 'one_at_a_time')
        
    Returns:
        int: Tag ID or None if failed
    """
    try:
        tag_label = f"episeerr_{rule_name}"
        headers = get_sonarr_headers()
        
        # Check if tag already exists
        tags_response = requests.get(f"{SONARR_URL}/api/v3/tag", headers=headers, timeout=10)
        
        if not tags_response.ok:
            logger.error(f"Failed to get tags. Status: {tags_response.status_code}")
            return None
        
        # Look for existing tag
        for tag in tags_response.json():
            if tag['label'].lower() == tag_label.lower():
                logger.debug(f"Found existing tag '{tag_label}' with ID {tag['id']}")
                return tag['id']
        
        # Tag doesn't exist - create it
        logger.info(f"Creating new tag '{tag_label}'")
        tag_create_response = requests.post(
            f"{SONARR_URL}/api/v3/tag",
            headers=headers,
            json={"label": tag_label},
            timeout=10
        )
        
        if tag_create_response.ok:
            tag_id = tag_create_response.json().get('id')
            logger.info(f"✓ Created tag '{tag_label}' with ID {tag_id}")
            return tag_id
        else:
            logger.error(f"Failed to create tag '{tag_label}': {tag_create_response.text}")
            return None
            
    except Exception as e:
        logger.error(f"Error creating rule tag '{rule_name}': {str(e)}")
        return None


def get_tag_mapping():
    """
    Get dict of tag_id → tag_name from Sonarr.
    
    Returns:
        dict: {tag_id: tag_label} or empty dict on failure
    """
    try:
        headers = get_sonarr_headers()
        response = requests.get(f"{SONARR_URL}/api/v3/tag", headers=headers, timeout=10)
        
        if response.ok:
            return {tag['id']: tag['label'] for tag in response.json()}
        else:
            logger.error(f"Failed to get tag mapping: {response.status_code}")
            return {}
            
    except Exception as e:
        logger.error(f"Error getting tag mapping: {str(e)}")
        return {}

def get_episeerr_delay_profile_id():
    """
    Find the custom delay profile that should contain episeerr tags.
    Looks for any profile that already has at least one episeerr_* tag.
    Falls back to first non-default profile if none found.
    """
    try:
        headers = get_sonarr_headers()
        resp = requests.get(f"{SONARR_URL}/api/v3/delayprofile", headers=headers, timeout=10)
        
        if not resp.ok:
            logger.error(f"Failed to get delay profiles: {resp.status_code}")
            return None
        
        profiles = resp.json()
        
        # Look for profile with any episeerr tag
        for p in profiles:
            tags = p.get('tags', [])
            if tags:
                # Get tag names
                tag_map = get_tag_mapping()
                tag_names = [tag_map.get(tid, '').lower() for tid in tags]
                if any(n.startswith('episeerr_') for n in tag_names):
                    logger.info(f"Using Episeerr delay profile ID {p['id']}")
                    return p['id']
        
        # No episeerr profile found → use first non-default (highest priority)
        for p in profiles:
            if p.get('order', 0) < 2147483647:  # default has max int order
                logger.info(f"No episeerr profile found - using first custom profile ID {p['id']}")
                return p['id']
        
        logger.warning("No suitable delay profile found")
        return None
        
    except Exception as e:
        logger.error(f"Error finding Episeerr delay profile: {str(e)}")
        return None



# ============================================================================
# NEW TAG SYSTEM: Control tags only in delay profile
# ============================================================================

def update_delay_profile_with_control_tags():
    """
    Update delay profile to ONLY include the three control tags:
    - episeerr_default
    - episeerr_select  
    - episeerr_delay
    
    Rule tags (episeerr_one_at_a_time, episeerr_get1keepseason, etc.) are 
    NOT included to allow immediate downloads after processing.
    
    Returns:
        bool: True if successful, False otherwise
    """
    logger.info("=== Updating delay profile with control tags only ===")
    
    try:
        profile_id = get_episeerr_delay_profile_id()
        if not profile_id:
            logger.warning("No custom delay profile found - skipping sync")
            return False
        
        headers = get_sonarr_headers()
        
        # Get current profile
        get_resp = requests.get(f"{SONARR_URL}/api/v3/delayprofile/{profile_id}", headers=headers)
        if not get_resp.ok:
            logger.error(f"Failed to get delay profile: {get_resp.status_code}")
            return False
        
        profile = get_resp.json()
        
        # Build set of ONLY the three control tags
        control_tags = set()
        
        # 1. episeerr_default
        default_id = create_episeerr_default_tag()
        if default_id:
            control_tags.add(default_id)
            logger.debug(f"Added episeerr_default (ID: {default_id})")
        else:
            logger.warning("Could not create/find episeerr_default tag")
        
        # 2. episeerr_select
        select_id = create_episeerr_select_tag()
        if select_id:
            control_tags.add(select_id)
            logger.debug(f"Added episeerr_select (ID: {select_id})")
        else:
            logger.warning("Could not create/find episeerr_select tag")
        
        # 3. episeerr_delay (NEW)
        delay_id = get_or_create_rule_tag_id('delay')
        if delay_id:
            control_tags.add(delay_id)
            logger.debug(f"Added episeerr_delay (ID: {delay_id})")
        else:
            logger.warning("Could not create/find episeerr_delay tag")
        
        # Update profile with ONLY these three tags
        profile['tags'] = list(control_tags)
        put_resp = requests.put(
            f"{SONARR_URL}/api/v3/delayprofile/{profile_id}",
            headers=headers,
            json=profile
        )
        
        if put_resp.ok:
            logger.info(f"✓ Updated delay profile with {len(control_tags)} control tags (default, select, delay)")
            logger.info(f"  Control tag IDs: {sorted(control_tags)}")
            return True
        else:
            logger.error(f"Failed to update delay profile: {put_resp.text}")
            return False
            
    except Exception as e:
        logger.error(f"Delay profile control tag update failed: {str(e)}")
        return False


def update_delay_with_all_episeerr_tags(config=None):
    """
    DEPRECATED - Use update_delay_profile_with_control_tags() instead.
    
    This function maintained for backward compatibility. The new system only adds
    control tags (default, select, delay) to the delay profile instead of all 
    rule tags, allowing immediate downloads after processing.
    """
    logger.info("Note: update_delay_with_all_episeerr_tags() now uses control tags only")
    return update_delay_profile_with_control_tags()





def get_series_from_sonarr(series_id):
    """
    Fetch series data from Sonarr API.
    
    Args:
        series_id: Sonarr series ID
        
    Returns:
        dict: Series data or None if failed/not found
    """
    try:
        headers = get_sonarr_headers()
        response = requests.get(
            f"{SONARR_URL}/api/v3/series/{series_id}",
            headers=headers,
            timeout=10
        )
        
        if response.ok:
            return response.json()
        elif response.status_code == 404:
            # Series doesn't exist - this is normal when series are deleted
            logger.debug(f"Series {series_id} not found in Sonarr (404)")
            return None
        else:
            logger.error(f"Failed to get series {series_id}: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"Error getting series {series_id}: {str(e)}")
        return None


def update_series_in_sonarr(series):
    """
    Update series in Sonarr (typically for tag changes).
    
    Args:
        series: Full series object from Sonarr
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        headers = get_sonarr_headers()
        response = requests.put(
            f"{SONARR_URL}/api/v3/series/{series['id']}",
            headers=headers,
            json=series,
            timeout=10
        )
        
        if response.ok:
            logger.debug(f"Updated series {series['id']} in Sonarr")
            return True
        else:
            logger.error(f"Failed to update series {series['id']}: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"Error updating series: {str(e)}")
        return False


def sync_rule_tag_to_sonarr(series_id, new_rule_name):
    """
    Update Sonarr tags to reflect rule assignment.
    - Remove ALL episeerr_* tags (except episeerr_select/default)
    - Add episeerr_<new_rule_name> tag
    - Leave all other user tags untouched
    
    Args:
        series_id: Sonarr series ID
        new_rule_name: Name of the rule to assign
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Get current series data
        series = get_series_from_sonarr(series_id)
        if not series:
            logger.debug(f"Series {series_id} not found for tag sync (likely deleted)")
            return False
        
        # Get tag mapping
        tag_mapping = get_tag_mapping()
        if not tag_mapping:
            logger.warning("Could not get tag mapping - proceeding without it")
            tag_mapping = {}
        
        # Get current tags
        current_tags = series.get('tags', [])
        updated_tags = []
        
        # Remove all episeerr_* tags (except episeerr_select which needs special handling)
        for tag_id in current_tags:
            tag_name = tag_mapping.get(tag_id, '').lower()
            
            # Keep if:
            # - Not an episeerr_ tag (user tags like 1080p, anime, etc.)
            # - OR is episeerr_select (needs special handling in webhook, removed separately)
            # Remove: episeerr_default (workflow complete), episeerr_<old_rulename> (old assignment)
            if not tag_name.startswith('episeerr_') or tag_name == 'episeerr_select':
                updated_tags.append(tag_id)
            else:
                logger.debug(f"Removing tag '{tag_name}' from series {series_id}")
        
        # Add new rule tag
        new_tag_id = get_or_create_rule_tag_id(new_rule_name)
        if new_tag_id:
            if new_tag_id not in updated_tags:
                updated_tags.append(new_tag_id)
                logger.info(f"Added tag 'episeerr_{new_rule_name}' to series {series_id}")
        else:
            logger.error(f"Failed to get/create tag for rule '{new_rule_name}'")
            return False
        
        # Update series with new tags
        series['tags'] = updated_tags
        return update_series_in_sonarr(series)
        
    except Exception as e:
        logger.error(f"Error syncing rule tag for series {series_id}: {str(e)}")
        return False


def remove_all_episeerr_tags(series_id):
    """
    Remove all episeerr_* tags from a series (used when unassigning).
    
    Args:
        series_id: Sonarr series ID
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        series = get_series_from_sonarr(series_id)
        if not series:
            return False
        
        tag_mapping = get_tag_mapping()
        current_tags = series.get('tags', [])
        
        # Keep only non-episeerr tags
        updated_tags = [
            tag_id for tag_id in current_tags
            if not tag_mapping.get(tag_id, '').lower().startswith('episeerr_')
        ]
        
        if len(updated_tags) != len(current_tags):
            series['tags'] = updated_tags
            logger.info(f"Removed episeerr tags from series {series_id}")
            return update_series_in_sonarr(series)
        
        return True
        
    except Exception as e:
        logger.error(f"Error removing episeerr tags from series {series_id}: {str(e)}")
        return False


def validate_series_tag(series_id, expected_rule):
    """
    Check if series tag matches expected rule in config.
    Handles multiple episeerr_* tags (logs error, auto-fixes to config rule).
    
    Args:
        series_id: Sonarr series ID
        expected_rule: Rule name from config
        
    Returns:
        tuple: (matches: bool, actual_tag_rule: str or None)
    """
    try:
        series = get_series_from_sonarr(series_id)
        if not series:
            return (False, None)
        
        tag_mapping = get_tag_mapping()
        
        # Find all episeerr_* tags (excluding special ones)
        episeerr_tags = []
        for tag_id in series.get('tags', []):
            tag_name = tag_mapping.get(tag_id, '')
            if tag_name.startswith('episeerr_'):
                rule_name = tag_name.replace('episeerr_', '')
                # Skip special workflow tags
                if rule_name not in ['select', 'default']:
                    episeerr_tags.append(rule_name)
        
        # No rule tags found
        if len(episeerr_tags) == 0:
            return (False, None)
        
        # Multiple rule tags (ERROR STATE)
        elif len(episeerr_tags) > 1:
            logger.error(f"Series {series_id} has multiple episeerr tags: {episeerr_tags}")
            # Auto-fix to expected rule
            sync_rule_tag_to_sonarr(series_id, expected_rule)
            return (False, expected_rule)
        
        # Single rule tag (NORMAL STATE)
        else:
            actual_rule = episeerr_tags[0]
            # FIXED: Case-insensitive comparison (Sonarr lowercases all tags)
            matches = (actual_rule.lower() == expected_rule.lower())
            return (matches, actual_rule)
            
    except Exception as e:
        logger.error(f"Error validating series tag for {series_id}: {str(e)}")
        return (False, None)
    



def unmonitor_series(series_id, headers):
    """Unmonitor all episodes in a series."""
    try:
        # Get all episodes for the series
        episodes_response = requests.get(
            f"{SONARR_URL}/api/v3/episode?seriesId={series_id}",
            headers=headers
        )
        
        if not episodes_response.ok:
            logger.error(f"Failed to get episodes. Status: {episodes_response.status_code}")
            return False

        episodes = episodes_response.json()
        all_episode_ids = [ep['id'] for ep in episodes]
        
        if all_episode_ids:
            unmonitor_response = requests.put(
                f"{SONARR_URL}/api/v3/episode/monitor",
                headers=headers,
                json={"episodeIds": all_episode_ids, "monitored": False}
            )
            
            if not unmonitor_response.ok:
                logger.error(f"Failed to unmonitor episodes. Status: {unmonitor_response.status_code}")
                return False
            else:
                logger.info(f"Unmonitored all episodes in series ID {series_id}")
                return True
        else:
            logger.info(f"No episodes found for series ID {series_id}")
            return True
            
    except Exception as e:
        logger.error(f"Error unmonitoring series: {str(e)}", exc_info=True)
        return False
    


def unmonitor_season(series_id, season_number, headers):
    """Unmonitor all episodes in a specific season."""
    try:
        # Get episodes for the specific season
        episodes_response = requests.get(
            f"{SONARR_URL}/api/v3/episode?seriesId={series_id}&seasonNumber={season_number}",
            headers=headers
        )
        
        if not episodes_response.ok:
            logger.error(f"Failed to get episodes. Status: {episodes_response.status_code}")
            return False

        episodes = episodes_response.json()
        season_episode_ids = [ep['id'] for ep in episodes]
        
        if season_episode_ids:
            unmonitor_response = requests.put(
                f"{SONARR_URL}/api/v3/episode/monitor",
                headers=headers,
                json={"episodeIds": season_episode_ids, "monitored": False}
            )
            
            if not unmonitor_response.ok:
                logger.error(f"Failed to unmonitor episodes. Status: {unmonitor_response.status_code}")
                return False
            else:
                logger.info(f"Unmonitored all episodes in series ID {series_id} season {season_number}")
                return True
        else:
            logger.info(f"No episodes found for series ID {series_id} season {season_number}")
            return True
            
    except Exception as e:
        logger.error(f"Error unmonitoring season: {str(e)}", exc_info=True)
        return False

def get_episode_info(episode_id, headers):
    """Get episode information from Sonarr API"""
    try:
        response = requests.get(f"{SONARR_URL}/api/v3/episode/{episode_id}", headers=headers)
        if response.ok:
            return response.json()
        return None
    except Exception as e:
        logger.error(f"Error getting episode info: {str(e)}")
        return None

def get_series_title(series_id, headers):
    """Get series title from Sonarr API"""
    try:
        response = requests.get(f"{SONARR_URL}/api/v3/series/{series_id}", headers=headers)
        if response.ok:
            return response.json().get('title', 'Unknown Series')
        return 'Unknown Series'
    except Exception as e:
        logger.error(f"Error getting series title: {str(e)}")
        return 'Unknown Series'

def cancel_download(queue_id, headers):
    """Cancel a download in Sonarr's queue"""
    try:
        # Primary method: Bulk remove with client removal
        payload = {
            "ids": [queue_id],
            "removeFromClient": True,
            "removeFromDownloadClient": True
        }
        response = requests.delete(f"{SONARR_URL}/api/v3/queue/bulk", headers=headers, json=payload)
        
        # If primary method fails, try alternative
        if not response.ok:
            logger.warning(f"Bulk removal failed for queue item {queue_id}. Trying alternative method.")
            response = requests.delete(
                f"{SONARR_URL}/api/v3/queue/{queue_id}", 
                headers=headers,
                params={
                    "removeFromClient": "true"
                }
            )
        
        return response.ok
    except Exception as e:
        logger.error(f"Error cancelling download: {str(e)}")
        return False

def monitor_specific_episodes(series_id, season_number, episode_numbers, headers):
    """
    Monitor specific episodes in a series season.
    
    :param series_id: Sonarr series ID
    :param season_number: Season number
    :param episode_numbers: List of episode numbers to monitor
    :param headers: Sonarr API headers
    :return: True if successful, False otherwise
    """
    try:
        episodes_response = requests.get(
            f"{SONARR_URL}/api/v3/episode?seriesId={series_id}",
            headers=headers
        )
        
        if not episodes_response.ok:
            logger.error(f"Failed to get episodes. Status: {episodes_response.status_code}")
            return False

        episodes = episodes_response.json()
        target_episodes = [
            ep for ep in episodes 
            if ep['seasonNumber'] == season_number and 
            ep['episodeNumber'] in episode_numbers
        ]
        
        if not target_episodes:
            logger.error(f"Target episodes {episode_numbers} not found in season {season_number}")
            return False
        
        monitor_episode_ids = [ep['id'] for ep in target_episodes]
        monitor_response = requests.put(
            f"{SONARR_URL}/api/v3/episode/monitor",
            headers=headers,
            json={"episodeIds": monitor_episode_ids, "monitored": True}
        )
        
        if not monitor_response.ok:
            logger.error(f"Failed to monitor episodes. Status: {monitor_response.status_code}")
            return False
        else:
            logger.info(f"Monitoring episodes {episode_numbers} in season {season_number}")
            return True
    
    except Exception as e:
        logger.error(f"Error monitoring specific episodes: {str(e)}", exc_info=True)
        return False

def search_episodes(series_id, episode_ids, headers):
    """
    Trigger a search for specific episodes in Sonarr.
    
    :param series_id: Sonarr series ID
    :param episode_ids: List of episode IDs to search for
    :param headers: Sonarr API headers
    :return: True if successful, False otherwise
    """
    try:
        if not episode_ids:
            logger.error("No episode IDs provided for search")
            return False
            
        logger.info(f"Searching for episodes: {episode_ids}")
            
        search_payload = {
            "name": "EpisodeSearch",
            "episodeIds": episode_ids
        }
        
        search_response = requests.post(
            f"{SONARR_URL}/api/v3/command",
            headers=headers,
            json=search_payload
        )
        
        if search_response.ok:
            logger.info(f"Triggered search for episodes {episode_ids}")
            return True
        else:
            logger.error(f"Failed to trigger search. Status: {search_response.status_code}")
            logger.error(f"Response content: {search_response.text}")
            return False
            
    except Exception as e:
        logger.error(f"Error searching for episodes: {str(e)}", exc_info=True)
        return False

def get_series_episodes(series_id, season_number, headers):
    """
    Get all episodes for a specific series and season.
    
    :param series_id: Sonarr series ID
    :param season_number: Season number
    :param headers: Sonarr API headers
    :return: List of episodes or empty list on failure
    """
    try:
        episodes_response = requests.get(
            f"{SONARR_URL}/api/v3/episode?seriesId={series_id}&seasonNumber={season_number}",
            headers=headers
        )
        
        if not episodes_response.ok:
            logger.error(f"Failed to get episodes. Status: {episodes_response.status_code}")
            return []

        return episodes_response.json()
        
    except Exception as e:
        logger.error(f"Error getting series episodes: {str(e)}", exc_info=True)
        return []

def get_overseerr_headers():
    """Get headers for Overseerr API requests."""
    api_key = os.getenv('OVERSEERR_API_KEY') or os.getenv('JELLYSEERR_API_KEY')
    return {
        'X-Api-Key': api_key,
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

def delete_overseerr_request(request_id):
    """
    Delete a specific request in Overseerr/Jellyseerr.
    
    :param request_id: ID of the request to delete
    :return: True if successful, False otherwise
    """
    try:
        # Try Overseerr URL first, then Jellyseerr URL as fallback
        overseerr_url = normalize_url(os.getenv('OVERSEERR_URL')) or normalize_url(os.getenv('JELLYSEERR_URL'))
        
        # Check if URL is configured
        if not overseerr_url:
            logger.warning(f"No Overseerr/Jellyseerr URL configured - skipping deletion of request {request_id}")
            return True  # Return True to allow processing to continue
            
        headers = get_overseerr_headers()
        
        # Log the deletion attempt
        logger.info(f"Attempting to delete Jellyseerr request {request_id}")
        logger.debug(f"Overseerr headers: {headers}")

        delete_response = requests.delete(
            f"{overseerr_url}/api/v1/request/{request_id}",
            headers=headers
        )
        
        # Log full response for debugging
        logger.debug(f"Delete Request Response Status: {delete_response.status_code}")
        try:
            response_json = delete_response.json()
            logger.debug(f"Delete Request Response JSON: {json.dumps(response_json, indent=2)}")
        except ValueError:
            logger.debug(f"Delete Request Response Text: {delete_response.text}")
        
        if delete_response.ok or delete_response.status_code == 404:
            # 404 means the request was already deleted
            logger.info(f"Successfully deleted or request not found: {request_id}")
            return True
        else:
            logger.error(f"Failed to delete request {request_id}. Status: {delete_response.status_code}")
            return False
    
    except Exception as e:
        logger.error(f"Error deleting Jellyseerr request: {str(e)}", exc_info=True)
        return False

def process_episode_selection(series_id, episode_numbers):
    """
    Fixed version that properly handles the season information
    """
    try:
        series_id = int(series_id)
        headers = get_sonarr_headers()
        
        # Get series info
        series_response = requests.get(
            f"{SONARR_URL}/api/v3/series/{series_id}",
            headers=headers
        )
        
        if not series_response.ok:
            logger.error(f"Failed to get series. Status: {series_response.status_code}")
            return False
            
        series = series_response.json()
        
        # Check pending_selections for season info
        season_number = None
        if str(series_id) in pending_selections:
            season_number = pending_selections[str(series_id)]['season']
        
        # If not in pending_selections, look through requests directory
        if season_number is None:
            for filename in os.listdir(REQUESTS_DIR):
                if filename.endswith('.json'):
                    try:
                        with open(os.path.join(REQUESTS_DIR, filename), 'r') as f:
                            request_data = json.load(f)
                            if request_data.get('series_id') == series_id:
                                season_number = request_data.get('season')
                                logger.info(f"Found season {season_number} from request file for series {series_id}")
                                break
                    except Exception as e:
                        logger.error(f"Error reading request file: {str(e)}")
        
        # Final fallback - get the latest season from Sonarr
        if season_number is None:
            seasons_response = requests.get(
                f"{SONARR_URL}/api/v3/series/{series_id}",
                headers=headers
            )
            
            if seasons_response.ok:
                series_data = seasons_response.json()
                if 'seasons' in series_data and series_data['seasons']:
                    # Get the highest season number
                    seasons = sorted([s.get('seasonNumber', 0) for s in series_data['seasons']])
                    if seasons:
                        season_number = seasons[-1]
                        logger.info(f"Using latest season {season_number} for series {series_id}")
        
        if season_number is None:
            logger.error(f"Could not determine season for series {series_id}")
            return False
        
        logger.info(f"Processing episode selection for {series['title']} Season {season_number}: {episode_numbers}")
        
        # Store in pending_selections for processing
        pending_selections[str(series_id)] = {
            'title': series['title'],
            'season': season_number,
            'episodes': [],
            'selected_episodes': set(episode_numbers)
        }
        
        # Get episode IDs for the season and selected episodes
        episodes = get_series_episodes(series_id, season_number, headers)
        
        if not episodes:
            logger.error(f"No episodes found for series {series_id} season {season_number}")
            return False
        
        # Filter to only selected episodes
        valid_episode_numbers = []
        for num in episode_numbers:
            if any(ep['episodeNumber'] == num for ep in episodes):
                valid_episode_numbers.append(num)
            else:
                logger.warning(f"Episode {num} not found in {series['title']} Season {season_number}")
        
        if not valid_episode_numbers:
            logger.error(f"No valid episodes found for selection {episode_numbers}")
            return False
        
        # Monitor selected episodes
        monitor_success = monitor_specific_episodes(
            series_id, 
            season_number, 
            valid_episode_numbers, 
            headers
        )
        
        if not monitor_success:
            logger.error(f"Failed to monitor episodes for series {series_id}")
            return False
        
        # Get episode IDs for searching
        episode_ids = [
            ep['id'] for ep in episodes 
            if ep['episodeNumber'] in valid_episode_numbers
        ]
        
        if not episode_ids:
            logger.error(f"Failed to find episode IDs for {valid_episode_numbers}")
            return False
        
        # Trigger search for the episodes
        search_success = search_episodes(series_id, episode_ids, headers)
        
        if search_success:
            logger.info(f"Successfully set up monitoring and search for {len(valid_episode_numbers)} episodes")
            return True
        else:
            logger.error(f"Failed to search for episodes")
            return False
            
    except Exception as e:
        logger.error(f"Error processing episode selection: {str(e)}", exc_info=True)
        return False

# Add this new function to your episeerr_utils.py file:

def process_episode_selection_with_season(series_id, season_number, episode_numbers):
    """
    Process episode selection with explicit season number - ENHANCED VERSION
    
    :param series_id: Sonarr series ID
    :param season_number: Explicit season number
    :param episode_numbers: List of episode numbers to monitor
    :return: True if successful, False otherwise
    """
    try:
        series_id = int(series_id)
        season_number = int(season_number)
        headers = get_sonarr_headers()
        
        logger.info(f"Processing episode selection for series {series_id}, season {season_number}, episodes {episode_numbers}")
        
        # Get series info
        series_response = requests.get(
            f"{SONARR_URL}/api/v3/series/{series_id}",
            headers=headers
        )
        
        if not series_response.ok:
            logger.error(f"Failed to get series. Status: {series_response.status_code}")
            return False
            
        series = series_response.json()
        logger.info(f"Processing episodes for {series['title']} Season {season_number}: {episode_numbers}")
        
        # Store in pending_selections for processing
        pending_selections[str(series_id)] = {
            'title': series['title'],
            'season': season_number,
            'episodes': [],
            'selected_episodes': set(episode_numbers)
        }
        
        # Get episode IDs for the season and selected episodes
        episodes = get_series_episodes(series_id, season_number, headers)
        
        if not episodes:
            logger.error(f"No episodes found for series {series_id} season {season_number}")
            return False
        
        # Filter to only selected episodes and log details
        valid_episode_numbers = []
        episode_ids_to_monitor = []
        
        for num in episode_numbers:
            matching_episode = next((ep for ep in episodes if ep['episodeNumber'] == num), None)
            if matching_episode:
                valid_episode_numbers.append(num)
                episode_ids_to_monitor.append(matching_episode['id'])
                logger.info(f"Found episode {num}: ID {matching_episode['id']} - {matching_episode.get('title', 'Unknown')}")
            else:
                logger.warning(f"Episode {num} not found in {series['title']} Season {season_number}")
        
        if not valid_episode_numbers:
            logger.error(f"No valid episodes found for selection {episode_numbers}")
            return False
        
        logger.info(f"Monitoring episodes: {valid_episode_numbers} (IDs: {episode_ids_to_monitor})")
        
        # Monitor selected episodes
        monitor_response = requests.put(
            f"{SONARR_URL}/api/v3/episode/monitor",
            headers=headers,
            json={"episodeIds": episode_ids_to_monitor, "monitored": True}
        )
        
        if not monitor_response.ok:
            logger.error(f"Failed to monitor episodes. Status: {monitor_response.status_code}")
            logger.error(f"Response: {monitor_response.text}")
            return False
        
        logger.info(f"Successfully monitored {len(episode_ids_to_monitor)} episodes")
        
        # Trigger search for the episodes
        search_payload = {
            "name": "EpisodeSearch",
            "episodeIds": episode_ids_to_monitor
        }
        
        search_response = requests.post(
            f"{SONARR_URL}/api/v3/command",
            headers=headers,
            json=search_payload
        )
        
        if search_response.ok:
            logger.info(f"Successfully triggered search for {len(episode_ids_to_monitor)} episodes")
            return True
        else:
            logger.error(f"Failed to search for episodes. Status: {search_response.status_code}")
            logger.error(f"Search response: {search_response.text}")
            return False
            
    except Exception as e:
        logger.error(f"Error processing episode selection: {str(e)}", exc_info=True)
        return False


def check_and_cancel_unmonitored_downloads():
    """
    Enhanced version with better episode selection awareness
    """
    headers = get_sonarr_headers()
    
    logger.info("Starting unmonitored download cancellation check")
    logger.info(f"Episodes tag ID: {EPISEERR_DEFAULT_TAG_ID}")
    
    try:
        # Retrieve current queue
        queue_response = requests.get(f"{SONARR_URL}/api/v3/queue", headers=headers)
        
        if not queue_response.ok:
            logger.error(f"Failed to retrieve queue. Status: {queue_response.status_code}")
            return
        
        queue = queue_response.json().get('records', [])
        logger.info(f"Total queue items: {len(queue)}")
        
        if len(queue) == 0:
            logger.info("No items in queue to process")
            return
            
        # Track cancelled items
        cancelled_count = 0
        
        for item in queue:
            # Detailed logging for each queue item
            logger.info(f"Examining queue item: {item.get('title', 'Unknown')}")
            logger.info(f"Series ID: {item.get('seriesId')}, Episode ID: {item.get('episodeId')}")
            
            # Check if this is a TV episode
            if item.get('seriesId') and item.get('episodeId'):
                # Get series details to check for episeerr tags
                series_response = requests.get(
                    f"{SONARR_URL}/api/v3/series/{item['seriesId']}", 
                    headers=headers
                )
                
                if not series_response.ok:
                    logger.error(f"Failed to get series details for ID {item['seriesId']}")
                    continue
                
                series = series_response.json()
                
                # Log series tags
                logger.info(f"Series tags: {series.get('tags', [])}")
                
                # Check if series has episeerr tags
                has_episeerr_tag = (
                    EPISEERR_DEFAULT_TAG_ID in series.get('tags', []) or
                    EPISEERR_SELECT_TAG_ID in series.get('tags', [])
                )
                
                if has_episeerr_tag:
                    # Get episode details
                    episode_info = get_episode_info(item['episodeId'], headers)
                    
                    if episode_info:
                        episode_number = episode_info.get('episodeNumber')
                        season_number = episode_info.get('seasonNumber')
                        logger.info(f"Episode details: S{season_number}E{episode_number}, Monitored: {episode_info.get('monitored')}")
                        
                        # Check pending_selections for this series first
                        series_id_str = str(item.get('seriesId'))
                        should_cancel = False
                        
                        if series_id_str in pending_selections:
                            series_info = pending_selections[series_id_str]
                            selected_episodes = series_info.get('selected_episodes', set())
                            selection_season = series_info.get('season')
                            
                            # If this episode is not in selected episodes for the selected season, cancel it
                            if season_number == selection_season and episode_number not in selected_episodes:
                                logger.info(f"Episode S{season_number}E{episode_number} not in selected episodes {selected_episodes}. Cancelling download.")
                                should_cancel = True
                            elif season_number != selection_season:
                                logger.info(f"Episode is from season {season_number}, but selection is for season {selection_season}. Cancelling download.")
                                should_cancel = True
                        
                        # If not in pending_selections, check monitored status
                        elif not episode_info.get('monitored', False):
                            logger.info(f"Episode S{season_number}E{episode_number} is unmonitored. Cancelling download.")
                            should_cancel = True
                        
                        if should_cancel:
                            cancel_success = cancel_download(item['id'], headers)
                            
                            if cancel_success:
                                series_title = series.get('title', 'Unknown Series')
                                logger.info(
                                    f"Cancelled download for episeerr series: "
                                    f"{series_title} - S{season_number}E{episode_number}"
                                )
                                cancelled_count += 1
                            else:
                                logger.error(f"Failed to cancel download for {series.get('title')} - Episode ID {item['episodeId']}")
                        else:
                            logger.info(f"Episode S{season_number}E{episode_number} is monitored/selected - keeping download")
                    else:
                        logger.warning(f"Could not get episode info for ID {item['episodeId']}")
                else:
                    logger.debug(f"Series {series.get('title')} already processed (no control tags) - skipping download queue check")
        
        # Log summary
        logger.info(f"Cancellation check complete. Cancelled {cancelled_count} unmonitored downloads for episeerr series")
    
    except Exception as e:
        logger.error(f"Error in download queue monitoring: {str(e)}", exc_info=True)
        
def save_request(series_id, title, season, episodes, request_id=None):
    """
    Save a request for episode selection to the requests directory.
    
    :param series_id: Sonarr series ID
    :param title: Series title
    :param season: Season number
    :param episodes: List of episode dictionaries
    :param request_id: Optional Jellyseerr request ID
    :return: Request ID string
    """
    try:
        # Create simplified episode list for display
        episode_list = [
            {
                'episodeNumber': ep.get('episodeNumber'),
                'title': ep.get('title', 'Unknown'),
                'id': ep.get('id')
            }
            for ep in episodes
        ]
        
        # Create request entry
        request_entry = {
            'id': f"{series_id}_{season}_{int(time.time())}",
            'series_id': series_id,
            'title': title,
            'season': season,
            'episodes': episode_list,
            'request_id': request_id,
            'selected_episodes': [],
            'created_at': int(time.time())
        }
        
        # Ensure the requests directory exists
        os.makedirs(REQUESTS_DIR, exist_ok=True)
        
        # Save request entry
        filename = f"{request_entry['id']}.json"
        with open(os.path.join(REQUESTS_DIR, filename), 'w') as f:
            json.dump(request_entry, f, indent=2)
        
        logger.info(f"Created request entry for {title} Season {season}")
        
        return request_entry['id']
        
    except Exception as e:
        logger.error(f"Error saving request: {str(e)}", exc_info=True)
        return None

# Initialize tags on import
#create_episeerr_default_tag()
#create_episeerr_select_tag()
import os
import requests
from datetime import datetime
from dotenv import load_dotenv
import logging
from episeerr_utils import normalize_url

# Load environment variables from .env file
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

# Configuration settings - DB first, fallback to .env
SONARR_URL, SONARR_API_KEY = get_sonarr_settings()
#MAX_SHOWS_ITEMS = int(os.getenv('MAX_SHOWS_ITEMS', 24))

# Setup logging
logger = logging.getLogger()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def load_preferences():
    """
    Load preferences for Sonarr configuration.
    Returns a dictionary containing Sonarr URL and API key.
    """
    # Fetch fresh from database each time
    SONARR_URL, SONARR_API_KEY = get_sonarr_settings()
    return {'SONARR_URL': SONARR_URL, 'SONARR_API_KEY': SONARR_API_KEY}

def fetch_episode_file_details(episode_file_id):
    episode_file_url = f"{SONARR_URL}/api/v3/episodefile/{episode_file_id}"
    headers = {'X-Api-Key': SONARR_API_KEY}
    response = requests.get(episode_file_url, headers=headers)
    return response.json() if response.ok else None

def get_episode(episode_id):
    """
    Get episode details from Sonarr by episode ID
    Used by notification system to check episode status
    
    Args:
        episode_id: Sonarr episode ID
        
    Returns:
        Episode object with metadata or None
    """
    preferences = load_preferences()
    headers = {'X-Api-Key': preferences['SONARR_API_KEY']}
    
    try:
        response = requests.get(
            f"{preferences['SONARR_URL']}/api/v3/episode/{episode_id}",
            headers=headers
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to get episode {episode_id}: {e}")
        return None
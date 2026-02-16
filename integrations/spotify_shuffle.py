# integrations/spotify_shuffle.py
"""
Spotify Random Shuffle Integration for Episeerr
================================================

Shows your Random Shuffle playlist stats and allows quick refresh
"""

from integrations.base import ServiceIntegration
from typing import Dict, Any, Optional, Tuple
import requests
import subprocess
import os
from datetime import datetime


class SpotifyShuffleIntegration(ServiceIntegration):
    """
    Integration for Spotify Random Shuffle
    Shows playlist stats and allows quick refresh from dashboard
    """
    
    # ==========================================
    # REQUIRED: Basic Service Information
    # ==========================================
    
    @property
    def service_name(self) -> str:
        return 'spotify_shuffle'
    
    @property
    def display_name(self) -> str:
        return 'Spotify Shuffle'
    
    @property
    def icon(self) -> str:
        return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/spotify.png'
    
    @property
    def description(self) -> str:
        return 'Random playlist generator - shows playlist stats and last refresh time'
    
    @property
    def category(self) -> str:
        return 'utility'
    
    @property
    def default_port(self) -> int:
        return 5000  # Web GUI port
    
    # ==========================================
    # REQUIRED: Connection Testing
    # ==========================================
    
    def test_connection(self, url: str, api_key: str) -> Tuple[bool, str]:
        """
        Test if the Spotify shuffle script is accessible
        api_key in this case is the script path
        """
        try:
            script_path = api_key  # Using api_key field to store script path
            
            if not os.path.exists(script_path):
                return False, f"Script not found at: {script_path}"
            
            # Check if config.json exists
            script_dir = os.path.dirname(script_path)
            config_path = os.path.join(script_dir, 'config.json')
            
            if not os.path.exists(config_path):
                return False, "config.json not found - run setup.py first"
            
            # Try to read config
            import json
            with open(config_path, 'r') as f:
                config = json.load(f)
            
            if not config.get('client_id') or not config.get('client_secret'):
                return False, "Spotify credentials not configured in config.json"
            
            # Check if we can reach the web GUI if URL is provided
            if url and url != 'http://localhost:5000':
                try:
                    response = requests.get(url, timeout=5)
                    if response.status_code == 200:
                        return True, "Connected to Spotify Shuffle Web GUI"
                except:
                    pass  # Web GUI not running is OK
            
            return True, "Spotify Shuffle configured and ready"
            
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    # ==========================================
    # REQUIRED: Dashboard Statistics
    # ==========================================
    
    def get_dashboard_stats(self, url: str, api_key: str) -> Dict[str, Any]:
        """
        Fetch stats about the Random Shuffle playlist
        """
        try:
            script_path = api_key
            script_dir = os.path.dirname(script_path)
            config_path = os.path.join(script_dir, 'config.json')
            cache_path = os.path.join(script_dir, '.cache-spotify')
            
            import json
            with open(config_path, 'r') as f:
                config = json.load(f)
            
            # Get stats from config
            num_tracks = config.get('num_tracks', 100)
            artist_limit = config.get('artist_limit', 4)
            last_run = config.get('last_run', 'Never')
            
            # Try to get actual playlist track count from Spotify
            try:
                import spotipy
                from spotipy.oauth2 import SpotifyOAuth
                
                auth_manager = SpotifyOAuth(
                    client_id=config.get('client_id'),
                    client_secret=config.get('client_secret'),
                    redirect_uri=config.get('redirect_uri', 'http://127.0.0.1:8888/callback'),
                    scope='playlist-read-private playlist-modify-private playlist-modify-public',
                    cache_path=cache_path,
                    open_browser=False
                )
                sp = spotipy.Spotify(auth_manager=auth_manager)
                
                # Find Random Shuffle playlist
                playlists = sp.current_user_playlists(limit=50)
                actual_tracks = 0
                for pl in playlists['items']:
                    if pl['name'] == 'Random Shuffle':
                        actual_tracks = pl['tracks']['total']
                        break
                
                return {
                    'configured': True,
                    'target_tracks': num_tracks,
                    'actual_tracks': actual_tracks,
                    'artist_limit': artist_limit,
                    'last_run': last_run
                }
            except Exception as e:
                # If we can't get actual count, return 0 but don't show error
                # (error in stats causes pill to hide)
                return {
                    'configured': True,
                    'target_tracks': num_tracks,
                    'actual_tracks': 0,
                    'artist_limit': artist_limit,
                    'last_run': last_run
                }
            
        except Exception as e:
            return {
                'configured': True,
                'error': str(e)
            }
    
    # ==========================================
    # REQUIRED: Dashboard Widget Configuration
    # ==========================================
    
    def get_dashboard_widget(self) -> Dict[str, Any]:
        """
        Define how Spotify Shuffle appears on dashboard
        """
        return {
            'enabled': True,
            'pill': {
                'icon': 'fas fa-shuffle',
                'icon_color': 'text-success',
                'template': '{actual_tracks} (max {artist_limit}/artist)',
                'fields': ['actual_tracks', 'artist_limit']
            }
        }
    
    # ==========================================
    # OPTIONAL: Custom Setup Fields
    # ==========================================
    
    def get_setup_fields(self) -> list:
        """
        Custom setup fields for Spotify Shuffle
        """
        return [
            {
                'name': 'url',
                'label': 'Web GUI URL',
                'type': 'text',
                'placeholder': 'http://localhost:5000',
                'help': 'URL to the Spotify Shuffle web interface (optional)'
            },
            {
                'name': 'api_key',
                'label': 'Script Path',
                'type': 'text',
                'placeholder': '/home/joe/projects/spotify_shuffle/importspotipy.py',
                'help': 'Full path to importspotipy.py script'
            }
        ]


# ==========================================
# REQUIRED: Export Integration Instance
# ==========================================

integration = SpotifyShuffleIntegration()
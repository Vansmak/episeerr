# integrations/spotify.py - FULLY SELF-CONTAINED
"""
Spotify Integration - Completely Self-Contained
No manual edits to episeerr.py or dashboard.html required!
"""

from integrations.base import ServiceIntegration
from typing import Dict, Any, Optional, Tuple
from flask import Blueprint, jsonify, request
import requests
import os
import json
import logging

logger = logging.getLogger(__name__)


class SpotifyIntegration(ServiceIntegration):
    """Self-contained Spotify integration with widget and playback controls"""
    
    # ==========================================
    # Service Information
    # ==========================================
    
    @property
    def service_name(self) -> str:
        return 'spotify'
    
    @property
    def display_name(self) -> str:
        return 'Spotify'
    
    @property
    def icon(self) -> str:
        return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/spotify.png'
    
    @property
    def description(self) -> str:
        return 'Music streaming with now playing widget and playback controls'
    
    @property
    def category(self) -> str:
        return 'dashboard'
    
    @property
    def default_port(self) -> int:
        return 443
    
    # ==========================================
    # Helper Methods
    # ==========================================
    
    # Replace the _get_token method in spotify.py:

    def _get_token(self, api_key: str) -> Optional[str]:
        """Get access token - NEVER trigger interactive auth"""
        try:
            if not api_key or not os.path.exists(api_key):
                return api_key  # Might be direct token
            
            cache_path = api_key
            
            # Read the cache file
            with open(cache_path, 'r') as f:
                cache_data = json.load(f)
            
            access_token = cache_data.get('access_token')
            expires_at = cache_data.get('expires_at', 0)
            refresh_token = cache_data.get('refresh_token')
            
            # Check if token is expired
            import time
            if expires_at > time.time():
                # Token is still valid
                return access_token
            
            # Token expired - try to refresh (non-interactive)
            if refresh_token:
                cache_dir = os.path.dirname(cache_path)
                config_path = os.path.join(cache_dir, 'config.json')
                
                if os.path.exists(config_path):
                    try:
                        import spotipy
                        from spotipy.oauth2 import SpotifyOAuth
                        
                        with open(config_path, 'r') as f:
                            config = json.load(f)
                        
                        auth_manager = SpotifyOAuth(
                            client_id=config.get('client_id'),
                            client_secret=config.get('client_secret'),
                            redirect_uri=config.get('redirect_uri', 'http://127.0.0.1:8888/callback'),
                            scope='user-library-read user-read-recently-played playlist-read-private user-read-playback-state user-modify-playback-state',
                            cache_path=cache_path,
                            open_browser=False
                        )
                        
                        # Use validate_token which refreshes without interactive prompt
                        token_info = auth_manager.validate_token(cache_data)
                        if token_info:
                            return token_info['access_token']
                        
                    except Exception as e:
                        logger.error(f"Token refresh failed: {e}")
            
            # Return expired token as last resort
            # (better to try and fail than trigger interactive auth)
            return access_token
            
        except Exception as e:
            logger.error(f"Token fetch error: {e}")
            return None
    # ==========================================
    # Required Methods
    # ==========================================
    
    def test_connection(self, url: str, api_key: str) -> Tuple[bool, str]:
        """Test Spotify connection"""
        try:
            token = self._get_token(api_key)
            if not token:
                return False, "No token available - check cache file path"
            
            headers = {'Authorization': f'Bearer {token}'}
            response = requests.get(
                'https://api.spotify.com/v1/me',
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                username = data.get('display_name', data.get('id'))
                return True, f"Connected as {username}"
            elif response.status_code == 401:
                return False, "Token expired - run shuffle script to refresh"
            else:
                return False, f"API error: HTTP {response.status_code}"
                
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    def get_dashboard_stats(self, url: str, api_key: str) -> Dict[str, Any]:
        """Get Spotify library statistics and now playing info"""
        try:
            token = self._get_token(api_key)
            if not token:
                return {'configured': True, 'error': 'No token'}
            
            headers = {'Authorization': f'Bearer {token}'}
            
            # Get user profile
            profile_response = requests.get(
                'https://api.spotify.com/v1/me',
                headers=headers,
                timeout=10
            )
            
            if profile_response.status_code != 200:
                return {'configured': True, 'error': 'Token expired'}
            
            profile = profile_response.json()
            
            # Get playlists count
            playlists_response = requests.get(
                'https://api.spotify.com/v1/me/playlists?limit=1',
                headers=headers,
                timeout=10
            )
            playlists_count = playlists_response.json().get('total', 0) if playlists_response.status_code == 200 else 0
            
            # Get saved tracks count
            tracks_response = requests.get(
                'https://api.spotify.com/v1/me/tracks?limit=1',
                headers=headers,
                timeout=10
            )
            saved_tracks = tracks_response.json().get('total', 0) if tracks_response.status_code == 200 else 0
            
            # Get current playback
            now_playing = None
            playback_response = requests.get(
                'https://api.spotify.com/v1/me/player',
                headers=headers,
                timeout=10
            )
            
            if playback_response.status_code == 200 and playback_response.text:
                playback = playback_response.json()
                if playback and playback.get('item'):  # Show current track even if paused
                    track = playback.get('item', {})
                    now_playing = {
                        'is_playing': playback.get('is_playing', False),  # CHANGED - use actual state
                        'track_name': track.get('name', 'Unknown'),
                        'artist_name': ', '.join([a['name'] for a in track.get('artists', [])]),
                        'album_art': track.get('album', {}).get('images', [{}])[0].get('url') if track.get('album', {}).get('images') else None
                    }
            # If nothing playing, get last played
            if not now_playing:
                recent_response = requests.get(
                    'https://api.spotify.com/v1/me/player/recently-played?limit=1',
                    headers=headers,
                    timeout=10
                )
                
                if recent_response.status_code == 200:
                    recent_data = recent_response.json()
                    if recent_data.get('items'):
                        last_track = recent_data['items'][0]
                        track = last_track.get('track', {})
                        now_playing = {
                           'is_playing': False,
                            'track_name': track.get('name', 'Unknown'),
                            'artist_name': ', '.join([a['name'] for a in track.get('artists', [])]),
                            'played_at': last_track.get('played_at')
                        }
            
            return {
                'configured': True,
                'playlists': playlists_count,
                'saved_tracks': saved_tracks,
                 'now_playing': now_playing
            }
            
        except Exception as e:
            logger.error(f"Spotify stats error: {e}")
            return {'configured': True, 'error': str(e)}
    
    def get_dashboard_widget(self) -> Dict[str, Any]:
        """Define dashboard pill"""
        return {
            'enabled': True,
            'pill': {
                'icon': 'fas fa-music',
                'icon_color': 'text-success',
                'template': '{playlists} • {saved_tracks}',
                'fields': ['playlists', 'saved_tracks']
            },
            'has_custom_widget': True  # Flag that this integration has a custom widget
        }
    
    # ==========================================
    # Self-Contained Routes (NEW!)
    # ==========================================
    
    def create_blueprint(self) -> Blueprint:
        """Create Flask blueprint with all Spotify-specific routes"""
        bp = Blueprint('spotify_integration', __name__)
        
        # Reference to self for use in route closures
        integration = self
        
        @bp.route('/api/integration/spotify/widget')
        def widget():
            """Get widget HTML"""
            try:
                from settings_db import get_service
                
                config = get_service('spotify', 'default')
                if not config or not config.get('enabled', True):
                    return jsonify({'success': False, 'message': 'Not enabled'})
                
                api_key = config.get('api_key', '')
                stats = integration.get_dashboard_stats('', api_key)
                
                if not stats or stats.get('error'):
                    return jsonify({'success': False, 'message': 'Stats error'})
                
                now_playing = stats.get('now_playing')
                if not now_playing:
                    return jsonify({'success': False, 'message': 'No playback data'})
                
                if now_playing.get('is_playing'):
                    album_art = f'<img src="{now_playing["album_art"]}" class="rounded" style="width: 80px; height: 80px; object-fit: cover; box-shadow: 0 2px 8px rgba(0,0,0,0.3);">' if now_playing.get('album_art') else ''
                    
                    html = f'''
                    <div class="card border-0 shadow-sm">
                        <div class="card-header bg-dark border-bottom">
                            <h6 class="mb-0">
                                <img src="{integration.icon}" style="width: 20px; height: 20px; margin-right: 8px;">
                                Now Playing
                            </h6>
                        </div>
                        <div class="card-body p-3">
                            <div class="d-flex gap-3 align-items-center">
                                {album_art}
                                <div class="flex-grow-1" style="min-width: 0;">
                                    <div class="fw-bold text-truncate mb-1" style="font-size: 15px;">
                                        {now_playing.get('track_name', 'Unknown')}
                                    </div>
                                    <div class="text-muted text-truncate" style="font-size: 13px;">
                                        {now_playing.get('artist_name', 'Unknown')}
                                    </div>
                                    <div class="mt-2">
                                        <span class="badge bg-success">
                                            <i class="fas fa-play me-1"></i>Playing
                                        </span>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                    '''
                else:
                    html = f'''
                    <div class="card border-0 shadow-sm">
                        <div class="card-header bg-dark border-bottom">
                            <h6 class="mb-0">
                                <img src="{integration.icon}" style="width: 20px; height: 20px; margin-right: 8px;">
                                Last Played
                            </h6>
                        </div>
                        <div class="card-body p-3">
                            <div class="text-center py-3">
                                <i class="fas fa-music text-muted mb-3" style="font-size: 2.5rem; opacity: 0.2;"></i>
                                <div class="fw-bold text-truncate mb-1" style="font-size: 14px;">
                                    {now_playing.get('track_name', 'Unknown')}
                                </div>
                                <div class="text-muted text-truncate" style="font-size: 12px;">
                                    {now_playing.get('artist_name', 'Unknown')}
                                </div>
                            </div>
                        </div>
                    </div>
                    '''
                
                return jsonify({'success': True, 'html': html})
                
            except Exception as e:
                logger.error(f"Widget error: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500
        
        @bp.route('/api/integration/spotify/control/<action>', methods=['POST'])
        def control(action):
            """Control playback"""
            try:
                from settings_db import get_service
                
                config = get_service('spotify', 'default')
                if not config:
                    return jsonify({'error': 'Not configured'}), 404
                
                api_key = config.get('api_key', '')
                token = integration._get_token(api_key)
                
                if not token:
                    return jsonify({'error': 'No token'}), 401
                
                headers = {'Authorization': f'Bearer {token}'}
                
                if action == 'pause':
                    response = requests.put('https://api.spotify.com/v1/me/player/pause', headers=headers, timeout=5)
                elif action == 'play':
                    response = requests.put('https://api.spotify.com/v1/me/player/play', headers=headers, timeout=5)
                elif action == 'next':
                    response = requests.post('https://api.spotify.com/v1/me/player/next', headers=headers, timeout=5)
                elif action == 'previous':
                    response = requests.post('https://api.spotify.com/v1/me/player/previous', headers=headers, timeout=5)
                else:
                    return jsonify({'error': f'Unknown action: {action}'}), 400
                
                if response.status_code in [200, 204]:
                    return jsonify({'success': True})
                else:
                    return jsonify({'error': f'Spotify API error: {response.status_code}'}), 400
                    
            except Exception as e:
                logger.error(f"Control error: {e}")
                return jsonify({'error': str(e)}), 500
        
        return bp
    
    def get_setup_fields(self) -> list:
        """Custom setup fields"""
        return [
            {
                'name': 'url',
                'label': 'Spotify Web URL (optional)',
                'type': 'text',
                'placeholder': 'https://open.spotify.com',
                'help': 'Link to Spotify web player (optional)'
            },
            {
                'name': 'api_key',
                'label': 'Cache File Path',
                'type': 'text',
                'placeholder': '/spotify_shuffle/.cache-spotify',
                'help': 'Path to your Spotify .cache file'
            }
        ]


# Export integration instance
integration = SpotifyIntegration()
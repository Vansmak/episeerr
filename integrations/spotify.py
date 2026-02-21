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
        """Get access token, auto-refreshing if expired"""
        import time
        import base64
        try:
            if not api_key or not os.path.exists(api_key):
                return api_key  # Might be a direct token string

            cache_path = api_key

            with open(cache_path, 'r') as f:
                cache_data = json.load(f)

            access_token = cache_data.get('access_token')
            expires_at = cache_data.get('expires_at', 0)
            refresh_token = cache_data.get('refresh_token')

            if expires_at > time.time():
                return access_token

            if not refresh_token:
                logger.warning("Spotify token expired and no refresh_token available")
                return access_token

            # Get client credentials - prefer settings_db, fall back to config.json
            client_id = None
            client_secret = None
            try:
                from settings_db import get_service
                svc = get_service('spotify', 'default') or {}
                cfg = svc.get('config') or {}
                client_id = cfg.get('client_id')
                client_secret = cfg.get('client_secret')
            except Exception:
                pass

            if not client_id or not client_secret:
                # Fall back to config.json next to the cache file
                config_path = os.path.join(os.path.dirname(cache_path), 'config.json')
                if os.path.exists(config_path):
                    try:
                        with open(config_path, 'r') as f:
                            file_cfg = json.load(f)
                        client_id = client_id or file_cfg.get('client_id')
                        client_secret = client_secret or file_cfg.get('client_secret')
                    except Exception:
                        pass

            if not client_id or not client_secret:
                logger.warning("Spotify token expired but no client_id/client_secret to refresh with")
                return access_token  # Return expired token; API will 401 but widget stays visible

            # Refresh via Spotify token endpoint (no spotipy needed)
            credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            resp = requests.post(
                'https://accounts.spotify.com/api/token',
                headers={
                    'Authorization': f'Basic {credentials}',
                    'Content-Type': 'application/x-www-form-urlencoded'
                },
                data={
                    'grant_type': 'refresh_token',
                    'refresh_token': refresh_token
                },
                timeout=10
            )

            if resp.status_code == 200:
                token_data = resp.json()
                new_access_token = token_data['access_token']
                new_expires_at = time.time() + token_data.get('expires_in', 3600)

                # Write refreshed token back to cache file
                cache_data['access_token'] = new_access_token
                cache_data['expires_at'] = new_expires_at
                if 'refresh_token' in token_data:
                    cache_data['refresh_token'] = token_data['refresh_token']
                try:
                    with open(cache_path, 'w') as f:
                        json.dump(cache_data, f)
                except Exception as write_err:
                    logger.warning(f"Could not write refreshed token to cache: {write_err}")

                logger.info("Spotify token refreshed successfully")
                return new_access_token
            else:
                logger.error(f"Spotify token refresh failed: {resp.status_code} {resp.text[:200]}")
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

                track_name = now_playing.get('track_name', 'Unknown')
                artist_name = now_playing.get('artist_name', 'Unknown')
                is_playing = now_playing.get('is_playing', False)
                album_art = f'<img src="{now_playing["album_art"]}" class="rounded" style="width:36px;height:36px;object-fit:cover;flex-shrink:0;">' if now_playing.get('album_art') else '<i class="fas fa-music text-success" style="font-size:16px;flex-shrink:0;opacity:0.7;"></i>'
                status_badge = '<span class="badge bg-success flex-shrink-0" style="font-size:10px;"><i class="fas fa-play me-1"></i>Playing</span>' if is_playing else '<span class="badge bg-secondary flex-shrink-0" style="font-size:10px;">Last played</span>'

                html = f'''
                <div class="d-flex align-items-center gap-2 px-2 py-1 rounded" style="background:rgba(255,255,255,0.04); min-height:36px;">
                    <img src="{integration.icon}" style="width:16px;height:16px;flex-shrink:0;">
                    {album_art}
                    <div class="flex-grow-1 overflow-hidden">
                        <div class="text-truncate fw-semibold" style="font-size:12px;line-height:1.2;">{track_name}</div>
                        <div class="text-truncate text-muted" style="font-size:11px;line-height:1.2;">{artist_name}</div>
                    </div>
                    {status_badge}
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
                'help': 'Path to your Spotify .cache file (contains the refresh token)'
            },
            {
                'name': 'client_id',
                'label': 'Spotify Client ID',
                'type': 'text',
                'placeholder': 'Your Spotify app client ID',
                'help': 'From developer.spotify.com — needed for automatic token refresh'
            },
            {
                'name': 'client_secret',
                'label': 'Spotify Client Secret',
                'type': 'password',
                'placeholder': 'Your Spotify app client secret',
                'help': 'From developer.spotify.com — needed for automatic token refresh'
            }
        ]


# Export integration instance
integration = SpotifyIntegration()
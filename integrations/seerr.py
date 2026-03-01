"""
Seerr Integration for Episeerr (Jellyseerr/Overseerr)
Handles: Webhook processing for media requests, auto-deletion after processing
"""

import os
import json
import requests
import logging
import time
from typing import Dict, Any, Optional, List
from flask import Blueprint, request, jsonify
from integrations.base import ServiceIntegration

logger = logging.getLogger(__name__)

REQUESTS_DIR = os.path.join(os.getcwd(), 'data', 'pending_requests')


class SeerrIntegration(ServiceIntegration):
    """Jellyseerr/Overseerr integration handler"""
    
    # ==========================================
    # Integration Metadata
    # ==========================================
    
    @property
    def service_name(self) -> str:
        return 'jellyseerr'  # Primary name
    
    @property
    def display_name(self) -> str:
        return 'Jellyseerr'
    
    @property
    def description(self) -> str:
        return 'Media request management - intercepts and stores requests and auto-deletes after processing'
    
    @property
    def icon(self) -> str:
        return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/jellyseerr.png'
    
    @property
    def category(self) -> str:
        return 'media'
    
    @property
    def default_port(self) -> int:
        return 5055
    
    # ==========================================
    # Setup Fields
    # ==========================================
    
    def get_setup_fields(self) -> Optional[List[Dict]]:
        """Custom setup fields for Jellyseerr/Overseerr"""
        return [
            {
                'name': 'url',
                'label': 'Jellyseerr/Overseerr URL',
                'type': 'text',
                'placeholder': 'http://192.168.1.100:5055',
                'required': True,
                'help_text': 'Your Jellyseerr or Overseerr server URL'
            },
            {
                'name': 'api_key',
                'label': 'API Key',
                'type': 'text',
                'placeholder': 'Enter API Key',
                'required': True,
                'help_text': 'Settings → General → API Key'
            }
        ]
    
    # ==========================================
    # Config Loading
    # ==========================================
    
    def get_config(self) -> Optional[Dict[str, Any]]:
        """Load Seerr configuration"""
        from settings_db import get_service
        service = get_service('jellyseerr', 'default')
        if service:
            return {
                'url': service['url'],
                'api_key': service['api_key']
            }
        return None
    
    # ==========================================
    # Test Connection
    # ==========================================
    
    def test_connection(self, url: str, api_key: str, **kwargs) -> tuple:
        """Test connection to Jellyseerr/Overseerr server"""
        try:
            headers = {'X-Api-Key': api_key}
            response = requests.get(f"{url}/api/v1/settings/public", headers=headers, timeout=10)
            
            if response.ok:
                data = response.json()
                app_name = data.get('applicationTitle', 'Jellyseerr')
                return True, f"Connected to {app_name}"
            else:
                return False, f"Server returned status {response.status_code}"
        
        except requests.exceptions.Timeout:
            return False, "Connection timeout - check URL and network"
        except requests.exceptions.ConnectionError:
            return False, "Cannot connect - check URL and that server is running"
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    # ==========================================
    # Request Management
    # ==========================================
    
    def get_tmdb_poster_path(self, tmdb_id: int) -> Optional[str]:
        """Get poster path from TMDB"""
        try:
            tmdb_api_key = os.getenv('TMDB_API_KEY')
            if not tmdb_api_key:
                return None
            
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
            response = requests.get(url, params={'api_key': tmdb_api_key}, timeout=10)
            
            if response.ok:
                data = response.json()
                poster_path = data.get('poster_path')
                if poster_path:
                    return f"https://image.tmdb.org/t/p/w500{poster_path}"
        except Exception as e:
            logger.error(f"Error fetching TMDB poster: {e}")
        
        return None
    
    def delete_request(self, request_id: int) -> bool:
        """Delete a request from Jellyseerr/Overseerr"""
        config = self.get_config()
        if not config:
            logger.error("Seerr not configured - cannot delete request")
            return False
        
        try:
            url = f"{config['url']}/api/v1/request/{request_id}"
            headers = {'X-Api-Key': config['api_key']}
            
            response = requests.delete(url, headers=headers, timeout=10)
            
            if response.ok:
                logger.info(f"✓ Deleted Jellyseerr request {request_id}")
                return True
            else:
                logger.error(f"Failed to delete request {request_id}: {response.status_code}")
                return False
        
        except Exception as e:
            logger.error(f"Error deleting Jellyseerr request {request_id}: {e}")
            return False
    
    # ==========================================
    # Dashboard
    # ==========================================
    
    def get_dashboard_widget(self) -> Optional[Dict]:
        """No dashboard widget for Seerr yet"""
        return None
    
    def get_dashboard_stats(self, url: str = None, api_key: str = None) -> Dict[str, Any]:
        """Get stats for dashboard"""
        return {'configured': True}
    
    # ==========================================
    # Flask Routes
    # ==========================================
    
    def create_blueprint(self) -> Blueprint:
        """Create Flask blueprint with Seerr-specific routes"""
        bp = Blueprint('seerr_integration', __name__, url_prefix='/api/integration/seerr')
        integration = self
        
        @bp.route('/webhook', methods=['POST'])
        def seerr_webhook():
            """
            Handle Jellyseerr/Overseerr webhooks
            
            Configure in Jellyseerr/Overseerr:
            Settings → Notifications → Webhook
            Webhook URL: http://<episeerr-ip>:5002/api/integration/seerr/webhook
            
            Stores request info and poster, auto-deletes after episode is added to Sonarr
            """
            try:
                logger.info("=== JELLYSEERR WEBHOOK RECEIVED ===")
                json_data = request.json
                
                logger.info(f"Jellyseerr webhook data: {json.dumps(json_data, indent=2)}")
                
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
                
                logger.info(f"Request ID: {request_id}, Media Type: {media_type}")
                
                # Only process TV show requests
                if media_type != 'tv':
                    logger.info(f"Request is not a TV show (media_type={media_type}), skipping")
                    return jsonify({"status": "success", "message": "Not a TV request"}), 200
                
                # Get identifiers
                tvdb_id = media_info.get('tvdbId')
                tmdb_id = media_info.get('tmdbId')
                title = json_data.get('subject', 'Unknown Show')
                
                logger.info(f"TVDB ID: {tvdb_id}, TMDB ID: {tmdb_id}, Title: {title}")
                
                # Extract requested seasons from webhook
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
                    poster_path = integration.get_tmdb_poster_path(tmdb_id) if tmdb_id else None

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
                    
                    logger.info(f"✓ Stored Jellyseerr request {request_id} for TVDB ID {tvdb_id_str} ({title}) - Seasons: {requested_seasons_str}")
                else:
                    logger.warning(f"Missing required data - TVDB ID: {tvdb_id}, Request ID: {request_id}")

                return jsonify({"status": "success"}), 200
                
            except Exception as e:
                logger.error(f"Error processing Jellyseerr webhook: {str(e)}", exc_info=True)
                return jsonify({"status": "error", "message": str(e)}), 500
        
        return bp


# Auto-discovery registration
integration = SeerrIntegration()

"""
SABnzbd Integration Plugin
Usenet download manager - queue status and speed

Drop this file in integrations/ and restart!
"""

from integrations.base import ServiceIntegration
import requests
import logging
from typing import Dict, Any, Tuple

logger = logging.getLogger(__name__)


class SABnzbdIntegration(ServiceIntegration):
    """SABnzbd integration for download queue stats"""
    
    # ============================================================
    # Required Properties
    # ============================================================
    
    @property
    def service_name(self) -> str:
        return 'sabnzbd'
    
    @property
    def display_name(self) -> str:
        return 'SABnzbd'
    
    @property
    def icon(self) -> str:
        return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/sabnzbd.png'
    
    @property
    def description(self) -> str:
        return 'Download queue status and speed'
    
    @property
    def category(self) -> str:
        return 'dashboard'
    
    @property
    def default_port(self) -> int:
        return 8080
    
    # ============================================================
    # Required Methods
    # ============================================================
    
    def test_connection(self, url: str, api_key: str) -> Tuple[bool, str]:
        """Test connection to SABnzbd"""
        try:
            response = requests.get(
                f"{url}/api",
                params={'mode': 'version', 'output': 'json', 'apikey': api_key},
                timeout=5
            )
            response.raise_for_status()
            
            data = response.json()
            version = data.get('version', 'unknown')
            
            return True, f"Connected to SABnzbd v{version}"
        
        except requests.exceptions.Timeout:
            return False, "Connection timeout - check URL and network"
        
        except requests.exceptions.ConnectionError:
            return False, "Cannot reach server - check URL"
        
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                return False, "Invalid API key"
            return False, f"HTTP error {e.response.status_code}"
        
        except Exception as e:
            logger.error(f"SABnzbd connection test error: {e}")
            return False, f"Error: {str(e)}"
    
    def get_dashboard_stats(self, url: str, api_key: str) -> Dict[str, Any]:
        """Get SABnzbd queue statistics for dashboard"""
        try:
            response = requests.get(
                f"{url}/api",
                params={'mode': 'queue', 'output': 'json', 'apikey': api_key},
                timeout=10
            )
            response.raise_for_status()
            
            data = response.json()
            queue = data.get('queue', {})
            
            return {
                'queue_count': queue.get('noofslots', 0),
                'speed': queue.get('speed', '0 B/s'),
                'size_left': queue.get('sizeleft', '0 B'),
                'size_left_mb': queue.get('mbleft', 0),
                'time_left': queue.get('timeleft', '0:00:00'),
                'paused': queue.get('paused', False),
                'status': queue.get('status', 'Idle'),
                'configured': True
            }
        
        except Exception as e:
            logger.error(f"Error fetching SABnzbd stats: {e}")
            return {
                'configured': True,
                'error': True,
                'error_message': str(e)
            }

    def get_dashboard_widget(self) -> Dict[str, Any]:
        """Define dashboard widget appearance"""
        return {
            'enabled': True,
            'pill': {
                'icon': 'fas fa-download',
                'icon_color': 'text-success',
                'template': '{queue_count} • {speed}',
                'fields': ['queue_count', 'speed']
            }
        }
# Export the integration instance
integration = SABnzbdIntegration()
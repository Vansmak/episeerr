"""
Radarr Integration Plugin
Movie library management and statistics

Drop this file in integrations/ and restart - that's it!
"""

from integrations.base import ServiceIntegration
import requests
import logging
from typing import Dict, Any, Tuple

logger = logging.getLogger(__name__)


class RadarrIntegration(ServiceIntegration):
    """Radarr integration for movie library stats and management"""
    
    # ============================================================
    # Required Properties
    # ============================================================
    
    @property
    def service_name(self) -> str:
        return 'radarr'
    
    @property
    def display_name(self) -> str:
        return 'Radarr'
    
    @property
    def icon(self) -> str:
        return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/radarr.png'
    
    @property
    def description(self) -> str:
        return 'Movie library stats and upcoming releases'
    
    @property
    def category(self) -> str:
        return 'dashboard'
    
    @property
    def default_port(self) -> int:
        return 7878
    
    # ============================================================
    # Required Methods
    # ============================================================
    
    def test_connection(self, url: str, api_key: str) -> Tuple[bool, str]:
        """Test connection to Radarr"""
        try:
            response = requests.get(
                f"{url}/api/v3/system/status",
                headers={'X-Api-Key': api_key},
                timeout=5
            )
            response.raise_for_status()
            
            data = response.json()
            version = data.get('version', 'unknown')
            
            return True, f"Connected to Radarr v{version}"
        
        except requests.exceptions.Timeout:
            return False, "Connection timeout - check URL and network"
        
        except requests.exceptions.ConnectionError:
            return False, "Cannot reach server - check URL"
        
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                return False, "Invalid API key"
            return False, f"HTTP error {e.response.status_code}"
        
        except Exception as e:
            logger.error(f"Radarr connection test error: {e}")
            return False, f"Error: {str(e)}"
    
    def get_dashboard_stats(self, url: str, api_key: str) -> Dict[str, Any]:
        """Get Radarr library statistics for dashboard"""
        try:
            # Get all movies
            response = requests.get(
                f"{url}/api/v3/movie",
                headers={'X-Api-Key': api_key},
                timeout=10
            )
            response.raise_for_status()
            movies = response.json()
            
            # Calculate stats
            movies_with_files = sum(1 for m in movies if m.get('hasFile', False))
            total_size = sum(m.get('sizeOnDisk', 0) for m in movies if m.get('hasFile', False))
            monitored = sum(1 for m in movies if m.get('monitored', False))
            
            return {
                'total_movies': len(movies),
                'downloaded_movies': movies_with_files,
                'monitored_movies': monitored,
                'size_on_disk': total_size,
                'size_gb': round(total_size / (1024**3), 2),
                'configured': True
            }
        
        except Exception as e:
            logger.error(f"Error fetching Radarr stats: {e}")
            return {
                'configured': True,
                'error': True,
                'error_message': str(e)
            }
    def get_dashboard_widget(self) -> Dict[str, Any]:
        """
        Define how this service appears on dashboard
        Returns widget configuration
        """
        return {
            'enabled': True,  # Show on dashboard?
            'pill': {
                'icon': 'fas fa-video',
                'icon_color': 'text-info',
                'template': '{total_movies} ({size_gb} GB)',
                'fields': ['total_movies', 'size_gb']
            }
        }

# Export the integration instance
# This is required for auto-discovery!
integration = RadarrIntegration()
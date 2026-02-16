"""
Prowlarr Integration Plugin
Indexer manager - health status and statistics

Drop this file in integrations/ and restart!
"""

from integrations.base import ServiceIntegration
import requests
import logging
from typing import Dict, Any, Tuple

logger = logging.getLogger(__name__)


class ProwlarrIntegration(ServiceIntegration):
    """Prowlarr integration for indexer health monitoring"""
    
    # ============================================================
    # Required Properties
    # ============================================================
    
    @property
    def service_name(self) -> str:
        return 'prowlarr'
    
    @property
    def display_name(self) -> str:
        return 'Prowlarr'
    
    @property
    def icon(self) -> str:
        return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/prowlarr.png'
    
    @property
    def description(self) -> str:
        return 'Indexer health and status'
    
    @property
    def category(self) -> str:
        return 'dashboard'
    
    @property
    def default_port(self) -> int:
        return 9696
    
    # ============================================================
    # Required Methods
    # ============================================================
    
    def test_connection(self, url: str, api_key: str) -> Tuple[bool, str]:
        """Test connection to Prowlarr"""
        try:
            response = requests.get(
                f"{url}/api/v1/system/status",
                headers={'X-Api-Key': api_key},
                timeout=5
            )
            response.raise_for_status()
            
            data = response.json()
            version = data.get('version', 'unknown')
            
            return True, f"Connected to Prowlarr v{version}"
        
        except requests.exceptions.Timeout:
            return False, "Connection timeout - check URL and network"
        
        except requests.exceptions.ConnectionError:
            return False, "Cannot reach server - check URL"
        
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                return False, "Invalid API key"
            return False, f"HTTP error {e.response.status_code}"
        
        except Exception as e:
            logger.error(f"Prowlarr connection test error: {e}")
            return False, f"Error: {str(e)}"
    
    def get_dashboard_stats(self, url: str, api_key: str) -> Dict[str, Any]:
        """Get Prowlarr indexer statistics for dashboard"""
        try:
            # Get indexers
            indexers_response = requests.get(
                f"{url}/api/v1/indexer",
                headers={'X-Api-Key': api_key},
                timeout=10
            )
            indexers_response.raise_for_status()
            indexers = indexers_response.json()
            
            enabled = sum(1 for i in indexers if i.get('enable', False))
            
            # Get health status
            health_response = requests.get(
                f"{url}/api/v1/health",
                headers={'X-Api-Key': api_key},
                timeout=10
            )
            health_response.raise_for_status()
            health = health_response.json()
            
            # Count issues
            issues = [h for h in health if h.get('type') in ['error', 'warning']]
            has_errors = any(h.get('type') == 'error' for h in issues)
            
            return {
                'total_indexers': len(indexers),
                'enabled_indexers': enabled,
                'disabled_indexers': len(indexers) - enabled,
                'health_issues': len(issues),
                'has_errors': has_errors,
                'status': 'error' if has_errors else ('warning' if issues else 'healthy'),
                'configured': True
            }
        
        except Exception as e:
            logger.error(f"Error fetching Prowlarr stats: {e}")
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
                'icon': 'fas fa-search-plus',
                'icon_color': 'text-primary',
                'template': '{enabled_indexers}/{total_indexers}',
                'fields': ['enabled_indexers', 'total_indexers']
            }
        }

# Export the integration instance
integration = ProwlarrIntegration()
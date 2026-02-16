# integrations/base.py - UPDATED BASE CLASS
"""
Base class for all integrations with self-contained route registration
"""

from typing import Dict, Any, Optional, Tuple, List
from flask import Blueprint


class ServiceIntegration:
    """
    Base class for service integrations
    All integrations should inherit from this
    """
    
    # ==========================================
    # REQUIRED: Service Information
    # ==========================================
    
    @property
    def service_name(self) -> str:
        """Unique service identifier (lowercase, no spaces)"""
        raise NotImplementedError
    
    @property
    def display_name(self) -> str:
        """Human-readable service name"""
        raise NotImplementedError
    
    @property
    def icon(self) -> str:
        """Icon URL (preferably CDN)"""
        raise NotImplementedError
    
    @property
    def description(self) -> str:
        """Short description of what this integration provides"""
        raise NotImplementedError
    
    @property
    def category(self) -> str:
        """Integration category: dashboard, notification, utility"""
        raise NotImplementedError
    
    @property
    def default_port(self) -> int:
        """Default port for this service"""
        raise NotImplementedError
    
    # ==========================================
    # REQUIRED: Connection & Stats
    # ==========================================
    
    def test_connection(self, url: str, api_key: str) -> Tuple[bool, str]:
        """Test connection to service"""
        raise NotImplementedError
    
    def get_dashboard_stats(self, url: str, api_key: str) -> Dict[str, Any]:
        """Get statistics for dashboard display"""
        raise NotImplementedError
    
    def get_dashboard_widget(self) -> Dict[str, Any]:
        """Define dashboard pill appearance"""
        raise NotImplementedError
    
    # ==========================================
    # OPTIONAL: Custom Routes (NEW!)
    # ==========================================
    
    def create_blueprint(self) -> Optional[Blueprint]:
        """
        Create a Flask Blueprint with custom routes for this integration
        This allows integrations to be completely self-contained
        
        Return None if no custom routes needed
        
        Example:
            bp = Blueprint(f'{self.service_name}_integration', __name__)
            
            @bp.route(f'/api/integration/{self.service_name}/widget')
            def widget():
                # Widget endpoint
                pass
            
            @bp.route(f'/api/integration/{self.service_name}/action', methods=['POST'])
            def action():
                # Control endpoint
                pass
            
            return bp
        """
        return None
    
    # ==========================================
    # OPTIONAL: Setup Fields
    # ==========================================
    
    def get_setup_fields(self) -> Optional[List[Dict]]:
        """
        Override default setup fields (URL + API Key)
        Return None to use defaults
        """
        return None
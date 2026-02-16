"""
Integration discovery and auto-registration system
"""

import os
import importlib
import logging
import traceback
from typing import List, Optional

logger = logging.getLogger(__name__)

# Storage
_integrations = {}
_integration_blueprints = []


def discover_integrations():
    """
    Discover all integration modules in the integrations directory
    Automatically registers their blueprints if they have custom routes
    """
    global _integrations, _integration_blueprints
    
    _integrations.clear()
    _integration_blueprints.clear()
    
    integrations_dir = os.path.dirname(__file__)
    logger.info(f"Scanning integrations directory: {integrations_dir}")
    
    py_files = [f for f in os.listdir(integrations_dir) if f.endswith('.py') and not f.startswith('_')]
    logger.info(f"Found {len(py_files)} potential integration files: {', '.join(py_files) or 'NONE'}")
    
    if not py_files:
        logger.warning("No .py files found in integrations/ (excluding __init__ and _*)")
        return
    
    for filename in py_files:
        module_name = filename[:-3]
        full_path = os.path.join(integrations_dir, filename)
        
        logger.debug(f"Attempting to load: {module_name} ({full_path})")
        
        try:
            # Import the module
            module = importlib.import_module(f'integrations.{module_name}')
            
            # Look for 'integration' instance
            if hasattr(module, 'integration'):
                integration = module.integration
                
                # Basic validation
                if not hasattr(integration, 'service_name') or not integration.service_name:
                    logger.error(f"✗ {module_name}: Missing or empty service_name")
                    continue
                
                service_name = integration.service_name
                display_name = getattr(integration, 'display_name', 'Unnamed')
                
                _integrations[service_name] = integration
                logger.info(f"✓ Loaded integration: {display_name} ({service_name})")
                
                # Blueprint support
                if hasattr(integration, 'create_blueprint'):
                    try:
                        blueprint = integration.create_blueprint()
                        if blueprint:
                            _integration_blueprints.append(blueprint)
                            logger.info(f"  → Blueprint registered for {service_name}")
                    except Exception as bp_err:
                        logger.error(f"  → Failed to create blueprint for {service_name}: {bp_err}")
            else:
                logger.warning(f"⚠️ {module_name}.py exists but has no 'integration' variable - skipping")
                
        except ImportError as ie:
            logger.error(f"✗ ImportError loading {module_name}: {ie}")
        except Exception as e:
            logger.error(f"✗ Unexpected error loading {module_name}: {e}")
            traceback.print_exc()   # ← This shows the full stack trace!
    
    logger.info(f"Final count: {_integrations and len(_integrations) or 0} integrations loaded")
    if _integrations:
        logger.info(f"Loaded services: {', '.join(_integrations.keys())}")
    else:
        logger.warning("No integrations were successfully loaded!")


def register_integration_blueprints(app):
    for blueprint in _integration_blueprints:
        app.register_blueprint(blueprint)
        logger.info(f"Registered blueprint: {blueprint.name}")


def get_integration(service_name: str):
    return _integrations.get(service_name)


def get_all_integrations() -> List:
    return list(_integrations.values())


def get_integrations_by_category(category: str) -> List:
    return [i for i in _integrations.values() if getattr(i, 'category', None) == category]


def reload_integrations():
    discover_integrations()


# Run discovery immediately
discover_integrations()
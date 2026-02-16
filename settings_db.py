"""
Settings Database - Store service configurations
Replaces env vars with database storage for easier management
"""

import sqlite3
import json
import os
from datetime import datetime
from typing import Optional, Dict, Any, List

DB_PATH = os.getenv('SETTINGS_DB_PATH', '/app/data/settings.db')

def init_settings_db():
    """Initialize settings database with all tables"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Services table - stores connection info for all external services
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_type TEXT NOT NULL,  -- 'sonarr', 'jellyfin', 'emby', 'plex', etc.
            name TEXT NOT NULL,          -- User-friendly name
            enabled BOOLEAN DEFAULT 1,
            url TEXT NOT NULL,
            api_key TEXT,
            config JSON,                 -- Service-specific config
            last_test TIMESTAMP,
            last_test_status TEXT,       -- 'success', 'failed', 'never_tested'
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(service_type, name)
        )
    ''')
    
    # Settings table - general app settings
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            category TEXT,               -- 'general', 'automation', 'ui', etc.
            description TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Quick links table - bookmarks to other services
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS quick_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            icon TEXT,                   -- Icon name or emoji
            order_index INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Quick links table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS quick_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            icon TEXT DEFAULT 'fas fa-link',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Migration: Add open_in_iframe column if it doesn't exist
    try:
        cursor.execute('SELECT open_in_iframe FROM quick_links LIMIT 1')
    except sqlite3.OperationalError:
        cursor.execute('ALTER TABLE quick_links ADD COLUMN open_in_iframe BOOLEAN DEFAULT 0')

    conn.commit()
    conn.close()

def get_service(service_type: str, name: str = 'default') -> Optional[Dict[str, Any]]:
    """Get a service configuration by type and name"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute(
        'SELECT * FROM services WHERE service_type = ? AND name = ? AND enabled = 1',
        (service_type, name)
    )
    
    row = cursor.fetchone()
    conn.close()
    
    if row:
        service = dict(row)
        if service['config']:
            service['config'] = json.loads(service['config'])
        return service
    return None

def get_all_services() -> List[Dict[str, Any]]:
    """Get all service configurations"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM services ORDER BY service_type, name')
    
    rows = cursor.fetchall()
    conn.close()
    
    services = []
    for row in rows:
        service = dict(row)
        if service['config']:
            service['config'] = json.loads(service['config'])
        services.append(service)
    
    return services

def save_service(service_type: str, name: str, url: str, api_key: str = None, 
                 config: Dict = None, enabled: bool = True) -> int:
    """Save or update a service configuration"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    config_json = json.dumps(config) if config else None
    
    cursor.execute('''
        INSERT INTO services (service_type, name, url, api_key, config, enabled, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(service_type, name) 
        DO UPDATE SET 
            url = excluded.url,
            api_key = excluded.api_key,
            config = excluded.config,
            enabled = excluded.enabled,
            updated_at = CURRENT_TIMESTAMP
    ''', (service_type, name, url, api_key, config_json, enabled))
    
    service_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return service_id

def update_service_test_result(service_type: str, name: str, status: str):
    """Update the last test result for a service"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE services 
        SET last_test = CURRENT_TIMESTAMP, last_test_status = ?
        WHERE service_type = ? AND name = ?
    ''', (status, service_type, name))
    
    conn.commit()
    conn.close()

def delete_service(service_type: str, name: str):
    """Delete a service configuration"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('DELETE FROM services WHERE service_type = ? AND name = ?', 
                   (service_type, name))
    
    conn.commit()
    conn.close()

def get_setting(key: str, default: Any = None) -> Any:
    """Get a setting value"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        # Try to parse as JSON for complex types
        try:
            return json.loads(row[0])
        except:
            return row[0]
    return default

def set_setting(key: str, value: Any, category: str = 'general', description: str = None):
    """Set a setting value"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Convert to JSON if not a string
    if not isinstance(value, str):
        value = json.dumps(value)
    
    cursor.execute('''
        INSERT INTO settings (key, value, category, description, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key)
        DO UPDATE SET 
            value = excluded.value,
            category = excluded.category,
            description = COALESCE(excluded.description, description),
            updated_at = CURRENT_TIMESTAMP
    ''', (key, value, category, description))
    
    conn.commit()
    conn.close()

def get_all_settings(category: str = None) -> Dict[str, Any]:
    """Get all settings, optionally filtered by category"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    if category:
        cursor.execute('SELECT * FROM settings WHERE category = ?', (category,))
    else:
        cursor.execute('SELECT * FROM settings')
    
    rows = cursor.fetchall()
    conn.close()
    
    settings = {}
    for row in rows:
        try:
            settings[row['key']] = json.loads(row['value'])
        except:
            settings[row['key']] = row['value']
    
    return settings

# Configuration getters with env fallback
def get_sonarr_config() -> Dict[str, str]:
    """Get Sonarr config from DB or env"""
    service = get_service('sonarr', 'default')
    if service:
        return {
            'url': service['url'],
            'api_key': service['api_key']
        }
    
    # Fallback to env
    return {
        'url': os.getenv('SONARR_URL'),
        'api_key': os.getenv('SONARR_API_KEY')
    }

def get_jellyfin_config() -> Optional[Dict[str, Any]]:
    """Get Jellyfin config from DB or env"""
    service = get_service('jellyfin', 'default')
    if service and service.get('config'):
        config = service['config']
        method = config.get('method', 'polling')  # Changed default from 'progress' to 'polling'
        
        # Base config
        base = {
            'url': service['url'],
            'api_key': service['api_key'],
            'user_id': config.get('user_id'),
            'method': method
        }
        
        # Add method-specific fields
        if method == 'polling':
            trigger_percent = config.get('trigger_percentage', config.get('trigger_percent', 50.0))
            base.update({
                'poll_interval': config.get('poll_interval', 900),
                'trigger_percentage': trigger_percent
            })
        else:  # progress mode
            base.update({
                'trigger_min': config.get('trigger_min', 50.0),
                'trigger_max': config.get('trigger_max', 55.0)
            })
        
        return base
    
    # Fallback to env
    if os.getenv('JELLYFIN_URL'):
        method = 'progress' if os.getenv('JELLYFIN_TRIGGER_MIN') else 'polling'
        base_config = {
            'url': os.getenv('JELLYFIN_URL'),
            'api_key': os.getenv('JELLYFIN_API_KEY'),
            'user_id': os.getenv('JELLYFIN_USER_ID'),
            'method': method
        }
        
        if method == 'polling':
            base_config.update({
                'poll_interval': int(os.getenv('JELLYFIN_POLL_INTERVAL', '900')),
                'trigger_percentage': float(os.getenv('JELLYFIN_TRIGGER_PERCENTAGE', os.getenv('JELLYFIN_TRIGGER_PERCENT', '50.0')))
            })
        else:
            base_config.update({
                'trigger_min': float(os.getenv('JELLYFIN_TRIGGER_MIN', '50.0')),
                'trigger_max': float(os.getenv('JELLYFIN_TRIGGER_MAX', '55.0'))
            })
        
        return base_config
    return None

def get_emby_config() -> Optional[Dict[str, Any]]:
    """Get Emby config from DB or env"""
    service = get_service('emby', 'default')
    if service and service.get('config'):
        return {
            'url': service['url'],
            'api_key': service['api_key'],
            'user_id': service['config'].get('user_id'),
            'poll_interval': service['config'].get('poll_interval', 900),
            'trigger_percentage': service['config'].get('trigger_percentage', 50.0)
        }
    
    # Fallback to env
    if os.getenv('EMBY_URL'):
        return {
            'url': os.getenv('EMBY_URL'),
            'api_key': os.getenv('EMBY_API_KEY'),
            'user_id': os.getenv('EMBY_USER_ID'),
            'poll_interval': int(os.getenv('EMBY_POLL_INTERVAL', '900')),
            'trigger_percentage': float(os.getenv('EMBY_TRIGGER_PERCENTAGE', '50.0'))
        }
    return None

# Quick Links Functions
def get_all_quick_links():
    """Get all quick links"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, name, url, icon, open_in_iframe, created_at
        FROM quick_links
        ORDER BY created_at ASC
    ''')
    
    links = []
    for row in cursor.fetchall():
        links.append({
            'id': row['id'],
            'name': row['name'],
            'url': row['url'],
            'icon': row['icon'],
            'open_in_iframe': bool(row['open_in_iframe']) if 'open_in_iframe' in row.keys() else False,
            'created_at': row['created_at']
        })
    
    conn.close()
    return links

def add_quick_link(name, url, icon='fas fa-link', open_in_iframe=False):
    """Add a new quick link"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO quick_links (name, url, icon, open_in_iframe)
        VALUES (?, ?, ?, ?)
    ''', (name, url, icon, 1 if open_in_iframe else 0))
    
    link_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return link_id

def delete_quick_link(link_id):
    """Delete a quick link"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('DELETE FROM quick_links WHERE id = ?', (link_id,))
    
    conn.commit()
    conn.close()
    
    return True

def get_quick_link_by_id(link_id):
    """Get a single quick link by ID"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM quick_links WHERE id = ?', (link_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return dict(row)
    return None



# Initialize database on import
init_settings_db()
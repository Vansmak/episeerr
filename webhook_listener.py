__version__ = "2.0.0"
from flask import Flask, render_template, request, redirect, url_for, jsonify
import subprocess
import os
import atexit
import re
import time
import logging
import json
import sonarr_utils
from flask import current_app
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import requests
import modified_episeerr
import threading
import shutil
from threading import Lock
from functools import lru_cache
from logging.handlers import RotatingFileHandler

app = Flask(__name__)

# Load environment variables
load_dotenv()
BASE_DIR = os.getcwd()
# Sonarr variables
SONARR_URL = os.getenv('SONARR_URL')
SONARR_API_KEY = os.getenv('SONARR_API_KEY')

# Jellyseerr variables
JELLYSEERR_URL = os.getenv('JELLYSEERR_URL', '')

# Global variable to track pending requests from Jellyseerr
# Format: {tvdb_id: {request_id: "123", title: "Show Title"}}
jellyseerr_pending_requests = {}

# Other settings
REQUESTS_DIR = os.path.join(os.getcwd(), 'data', 'requests')
os.makedirs(REQUESTS_DIR, exist_ok=True)

LAST_PROCESSED_FILE = os.path.join(os.getcwd(), 'data', 'last_processed.json')
os.makedirs(os.path.dirname(LAST_PROCESSED_FILE), exist_ok=True)

# Setup logging to capture all logs
log_file = os.getenv('LOG_PATH', os.path.join(os.getcwd(), 'logs', 'app.log'))

log_level = logging.INFO  # Capture INFO and ERROR levels

# Create log directory if it doesn't exist
os.makedirs(os.path.dirname(log_file), exist_ok=True)

# Create a RotatingFileHandler
file_handler = RotatingFileHandler(
    log_file,
    maxBytes=1*1024*1024,  # 1 MB max size
    backupCount=2,  # Keep 2 backup files
    encoding='utf-8'
)
file_handler.setLevel(log_level)
file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)

# Configure the root logger
logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[file_handler]
)

# Adding stream handler to also log to console for Docker logs to capture
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.DEBUG if os.getenv('FLASK_DEBUG', 'false').lower() == 'true' else logging.INFO)
formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(message)s')
stream_handler.setFormatter(formatter)
app.logger.addHandler(stream_handler)

# Configuration management
config_path = os.path.join(app.root_path, 'config', 'config.json')

class OCDarrScheduler:
    """Simple internal scheduler - FIXED."""
    
    def __init__(self):
        self.cleanup_thread = None
        self.running = False
        self.cleanup_interval_hours = int(os.getenv('CLEANUP_INTERVAL_HOURS', '6'))
        self.last_cleanup = 0
        
    def start_scheduler(self):
        """Start the cleanup scheduler."""
        if self.running:
            return
            
        self.running = True
        self.cleanup_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.cleanup_thread.start()
        
        print(f"✓ Internal scheduler started - cleanup every {self.cleanup_interval_hours} hours")
    
    def _scheduler_loop(self):
        """Main scheduler loop - runs in background."""
        time.sleep(300)  # Wait 5 minutes after startup
        
        while self.running:
            try:
                current_time = time.time()
                hours_since_last = (current_time - self.last_cleanup) / 3600
                
                if hours_since_last >= self.cleanup_interval_hours:
                    print("⏰ Starting scheduled cleanup...")
                    self._run_cleanup()
                    self.last_cleanup = current_time
                
                time.sleep(600)  # Check every 10 minutes
                
            except Exception as e:
                print(f"Scheduler error: {str(e)}")
                time.sleep(300)
    
    def _run_cleanup(self):
        """Execute cleanup in separate thread."""
        try:
            import servertosonarr
            servertosonarr.run_periodic_cleanup()
            print("✓ Scheduled cleanup completed")
        except Exception as e:
            print(f"Cleanup failed: {str(e)}")
    
    def force_cleanup(self):
        """Manually trigger cleanup."""
        cleanup_thread = threading.Thread(target=self._run_cleanup, daemon=True)
        cleanup_thread.start()
        return "Cleanup started"
    
    def get_status(self):
        """Get scheduler status."""
        if not self.running:
            return {"status": "stopped", "next_cleanup": None}
        
        if self.last_cleanup == 0:
            next_cleanup = "5 minutes after startup"
        else:
            next_time = self.last_cleanup + (self.cleanup_interval_hours * 3600)
            next_cleanup = datetime.fromtimestamp(next_time).strftime("%Y-%m-%d %H:%M:%S")
        
        return {
            "status": "running",
            "interval_hours": self.cleanup_interval_hours,
            "last_cleanup": datetime.fromtimestamp(self.last_cleanup).strftime("%Y-%m-%d %H:%M:%S") if self.last_cleanup else "Never",
            "next_cleanup": next_cleanup
        }
# Enhanced logging setup for cleanup operations
def setup_cleanup_logging():
    """Setup dedicated cleanup logging that writes to both console and files."""
    
    LOG_PATH = os.getenv('LOG_PATH', '/app/logs/app.log')
    CLEANUP_LOG_PATH = os.getenv('CLEANUP_LOG_PATH', '/app/logs/cleanup.log')
    
    # Ensure log directory exists
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(CLEANUP_LOG_PATH), exist_ok=True)
    
    # Create cleanup-specific logger
    cleanup_logger = logging.getLogger('cleanup')
    cleanup_logger.setLevel(logging.INFO)
    
    # Clear any existing handlers
    cleanup_logger.handlers.clear()
    
    # File handler for main app log
    main_file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=10*1024*1024,  # 10 MB
        backupCount=3,
        encoding='utf-8'
    )
    main_file_handler.setLevel(logging.INFO)
    main_file_formatter = logging.Formatter('%(asctime)s - CLEANUP - %(levelname)s - %(message)s')
    main_file_handler.setFormatter(main_file_formatter)
    
    # Dedicated cleanup file handler
    cleanup_file_handler = RotatingFileHandler(
        CLEANUP_LOG_PATH,
        maxBytes=5*1024*1024,  # 5 MB
        backupCount=5,
        encoding='utf-8'
    )
    cleanup_file_handler.setLevel(logging.INFO)
    cleanup_file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    cleanup_file_handler.setFormatter(cleanup_file_formatter)
    
    # Console handler for Docker logs
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(asctime)s - CLEANUP - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)
    
    # Add all handlers to cleanup logger
    cleanup_logger.addHandler(main_file_handler)
    cleanup_logger.addHandler(cleanup_file_handler)
    cleanup_logger.addHandler(console_handler)
    
    # Prevent propagation to root logger to avoid duplicate messages
    cleanup_logger.propagate = False
    
    return cleanup_logger

# Initialize cleanup logger
cleanup_logger = setup_cleanup_logging()

def migrate_config_complete(config_path):
    """
    Complete migration from old to new config format:
    1. Field names: keep_watched_days → grace_days, keep_unwatched_days → dormant_days
    2. Series structure: array → dict with activity_date
    3. Add dry_run field to rules
    """
    try:
        # Read current config
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        # Check if already migrated
        if config.get('field_migration_complete') and config.get('series_migration_complete'):
            print("✅ Config already fully migrated")
            return config
        
        # Create backup
        backup_path = config_path + f'.backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
        shutil.copy2(config_path, backup_path)
        print(f"📁 Created backup: {backup_path}")
        
        # Track what we migrated
        field_migrations = 0
        series_migrations = 0
        
        # Migrate each rule
        for rule_name, rule in config.get('rules', {}).items():
            print(f"\n🔄 Migrating rule: {rule_name}")
            
            # 1. Migrate field names
            if 'keep_watched_days' in rule:
                rule['grace_days'] = rule.pop('keep_watched_days')
                field_migrations += 1
                print(f"  ✓ Migrated keep_watched_days → grace_days")
            
            if 'keep_unwatched_days' in rule:
                rule['dormant_days'] = rule.pop('keep_unwatched_days')
                field_migrations += 1
                print(f"  ✓ Migrated keep_unwatched_days → dormant_days")
            
            # Ensure new fields exist
            if 'grace_days' not in rule:
                rule['grace_days'] = None
            if 'dormant_days' not in rule:
                rule['dormant_days'] = None
            
            # 2. Migrate series structure from array to dict
            if 'series' in rule and isinstance(rule['series'], list):
                old_series = rule['series']
                new_series = {}
                
                for series_id in old_series:
                    # Convert to string and add activity_date
                    new_series[str(series_id)] = {
                        'activity_date': None
                    }
                
                rule['series'] = new_series
                series_migrations += 1
                print(f"  ✓ Migrated {len(old_series)} series from array to dict format")
            
            # 3. Add dry_run field if missing
            if 'dry_run' not in rule:
                rule['dry_run'] = False
                print(f"  ✓ Added dry_run field (default: False)")
        
        # Mark migrations complete
        config['field_migration_complete'] = True
        config['series_migration_complete'] = True
        
        # Save migrated config
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=4)
        
        print(f"\n✅ Migration complete!")
        print(f"   - Field migrations: {field_migrations}")
        print(f"   - Series structure migrations: {series_migrations}")
        print(f"   - Backup saved to: {backup_path}")
        
        return config
        
    except Exception as e:
        print(f"❌ Migration failed: {str(e)}")
        raise


# Integration function for your existing code
def load_config_with_migration(config_path):
    """
    Load config with automatic migration.
    Use this instead of plain json.load() in your code.
    """
    if not os.path.exists(config_path):
        # Create default config if it doesn't exist
        default_config = {
            "rules": {
                "default": {
                    "get_option": "1",
                    "action_option": "search",
                    "keep_watched": "1",
                    "monitor_watched": False,
                    "grace_days": None,
                    "dormant_days": None,
                    "series": {},
                    "dry_run": False
                }
            },
            "default_rule": "default",
            "field_migration_complete": True,
            "series_migration_complete": True
        }
        
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, 'w') as f:
            json.dump(default_config, f, indent=4)
        
        return default_config
    
    # Migrate if needed
    return migrate_config_complete(config_path)

def load_config():
    """Load configuration from JSON file."""
    try:
        with open(config_path, 'r') as file:
            config = json.load(file)
        if 'rules' not in config:
            config['rules'] = {}
        
        # Run the complete migration
        config = migrate_config_complete(config_path)
        
        return config
    except FileNotFoundError:
        # Default config with new field names only
        default_config = {
            'rules': {
                'full_seasons': {
                    'get_option': 'season',
                    'action_option': 'monitor',
                    'keep_watched': 'season',
                    'monitor_watched': False,
                    'grace_days': None,
                    'dormant_days': None,
                    'series': {},
                    'dry_run': False
                }
            },
            'default_rule': 'full_seasons',
            'field_migration_complete': True,
            'series_migration_complete': True
        }
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        save_config(default_config)
        return default_config


  
def save_config(config):
    """Save configuration to JSON file with debugging."""
    config_path = os.path.join(os.getcwd(), 'config', 'config.json')  # FIXED: Proper path
    
    print(f"DEBUG: save_config called")
    print(f"DEBUG: config_path = {config_path}")
    print(f"DEBUG: Config to save: {json.dumps(config, indent=2)}")
    
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, 'w') as file:
            json.dump(config, file, indent=4)
        print(f"DEBUG: Config saved successfully to {config_path}")
        
        # Verify it was saved
        with open(config_path, 'r') as file:
            saved_config = json.load(file)
        print(f"DEBUG: Verified save - dry_run status: {saved_config.get('rules', {}).get('nukeafter90days', {}).get('dry_run', 'NOT_FOUND')}")
        
    except Exception as e:
        print(f"DEBUG: Save failed: {str(e)}")
        raise
def get_sonarr_series():
    """Get all series from Sonarr."""
    try:
        sonarr_preferences = sonarr_utils.load_preferences()
        headers = {
            'X-Api-Key': sonarr_preferences['SONARR_API_KEY'],
            'Content-Type': 'application/json'
        }
        sonarr_url = sonarr_preferences['SONARR_URL']
        
        response = requests.get(f"{sonarr_url}/api/v3/series", headers=headers)
        if response.ok:
            return response.json()
        else:
            app.logger.error(f"Failed to fetch series from Sonarr: {response.status_code}")
            return []
    except Exception as e:
        app.logger.error(f"Error fetching Sonarr series: {str(e)}")
        return []

# =============================================================================
# WEB UI ROUTES (UNCHANGED)
# =============================================================================



@app.route('/')
def index():
    """Main rules management page."""
    config = load_config()
    
    all_series = get_sonarr_series()
    
    rules_mapping = {}
    for rule_name, details in config['rules'].items():
        series_dict = details.get('series', {})  # NOW it's a dict
        for series_id in series_dict.keys():     # Get the keys
            rules_mapping[str(series_id)] = rule_name
    
    for series in all_series:
        series['assigned_rule'] = rules_mapping.get(str(series['id']), 'None')
    
    all_series.sort(key=lambda x: x.get('title', '').lower())
    
    return render_template('rules_index.html', 
                         config=config,
                         all_series=all_series,
                         current_rule=request.args.get('rule', list(config['rules'].keys())[0] if config['rules'] else 'full_seasons'))

@app.route('/create-rule', methods=['GET', 'POST'])
def create_rule():
    """Create a new rule."""
    if request.method == 'POST':
        config = load_config()
       
        rule_name = request.form.get('rule_name', '').strip()
        if not rule_name:
            return redirect(url_for('index', message="Rule name is required"))
       
        if rule_name in config['rules']:
            return redirect(url_for('index', message=f"Rule '{rule_name}' already exists"))
       
        # Handle time-based fields
        grace_days = request.form.get('grace_days', '').strip()
        dormant_days = request.form.get('dormant_days', '').strip()
       
        # Convert empty strings to None, otherwise convert to int
        grace_days = None if not grace_days else int(grace_days)      # FIXED: Actually convert to int
        dormant_days = None if not dormant_days else int(dormant_days)  # FIXED: Actually convert to int
       
        config['rules'][rule_name] = {
            'get_option': request.form.get('get_option', ''),
            'action_option': request.form.get('action_option', 'monitor'),
            'keep_watched': request.form.get('keep_watched', ''),
            'monitor_watched': request.form.get('monitor_watched') == 'on',  # FIXED: Use == 'on'
            'grace_days': grace_days,     # Now will be int or None
            'dormant_days': dormant_days, # Now will be int or None
            'series': {}                  # FIXED: Use dict not list
        }
       
        save_config(config)
        return redirect(url_for('index', message=f"Rule '{rule_name}' created successfully"))
   
    return render_template('create_rule.html')

@app.route('/edit-rule/<rule_name>', methods=['GET', 'POST'])
def edit_rule(rule_name):
    """Edit an existing rule."""
    config = load_config()
   
    if rule_name not in config['rules']:
        return redirect(url_for('index', message=f"Rule '{rule_name}' not found"))
   
    if request.method == 'POST':
        # Handle time-based fields
        grace_days = request.form.get('grace_days', '').strip()
        dormant_days = request.form.get('dormant_days', '').strip()
       
        # Convert empty strings to None, otherwise convert to int
        grace_days = None if not grace_days else int(grace_days)
        dormant_days = None if not dormant_days else int(dormant_days)
       
        config['rules'][rule_name].update({
            'get_option': request.form.get('get_option', ''),
            'action_option': request.form.get('action_option', 'monitor'),
            'keep_watched': request.form.get('keep_watched', ''),
            'monitor_watched': request.form.get('monitor_watched', 'false').lower() == 'true',
            'grace_days': grace_days,
            'dormant_days': dormant_days
        })
       
        save_config(config)
        return redirect(url_for('index', message=f"Rule '{rule_name}' updated successfully"))
   
    rule = config['rules'][rule_name]
    return render_template('edit_rule.html', rule_name=rule_name, rule=rule)

@app.route('/delete-rule/<rule_name>', methods=['POST'])
def delete_rule(rule_name):
    """Delete a rule."""
    config = load_config()
    
    if rule_name not in config['rules']:
        return redirect(url_for('index', message=f"Rule '{rule_name}' not found"))
    
    if rule_name == config.get('default_rule'):
        return redirect(url_for('index', message="Cannot delete the default rule"))
    
    del config['rules'][rule_name]
    save_config(config)
    
    return redirect(url_for('index', message=f"Rule '{rule_name}' deleted successfully"))

# Enhanced webhook_listener.py - Series Assignment Date Tracking


@app.route('/assign-rules', methods=['POST'])
def assign_rules():
    """Assign series to rules - FIXED for dict structure."""
    config = load_config()
    
    rule_name = request.form.get('rule_name')
    series_ids = request.form.getlist('series_ids')
    
    if not rule_name or rule_name not in config['rules']:
        return redirect(url_for('index', message="Invalid rule selected"))
    
    # FIXED: Remove series from all other rules (handle dict format)
    for rule, details in config['rules'].items():
        series_dict = details.get('series', {})
        for series_id in series_ids:
            if series_id in series_dict:
                del series_dict[series_id]
    
    # FIXED: Add to selected rule using dict format
    target_series_dict = config['rules'][rule_name].get('series', {})
    for series_id in series_ids:
        target_series_dict[series_id] = {'activity_date': None}
    
    save_config(config)
    
    return redirect(url_for('index', message=f"Assigned {len(series_ids)} series to rule '{rule_name}'"))
@app.route('/set-default-rule', methods=['POST'])
def set_default_rule():
    """Enhanced default rule setting with tracking for all assigned series."""
    config = load_config()
    
    rule_name = request.form.get('rule_name')
    
    if rule_name not in config['rules']:
        return redirect(url_for('index', message="Invalid rule selected"))
    
    config['default_rule'] = rule_name
    save_config(config)
    
    # Track assignment date for all series currently assigned to this rule
    assigned_series = config['rules'][rule_name].get('series', [])
    for series_id in assigned_series:
        track_rule_assignment(series_id, rule_name)
    
    return redirect(url_for('index', message=f"Set '{rule_name}' as default rule and tracked assignment dates"))
@app.route('/unassign-series', methods=['POST'])
def unassign_series():
    """Remove series from all rules."""
    config = load_config()
    
    series_ids = request.form.getlist('series_ids')
    
    total_removed = 0
    for rule_name, details in config['rules'].items():
        original_count = len(details.get('series', []))
        series_dict = details.get('series', {})
        for series_id in series_ids:
            if series_id in series_dict:
                del series_dict[series_id]
        total_removed += original_count - len(details['series'])
    
    save_config(config)
    
    return redirect(url_for('index', message=f"Unassigned {len(series_ids)} series from all rules"))



@app.route('/api/recent-activity')
def get_recent_activity():
    """Get recent rule applications and downloads for banner updates."""
    try:
        # This could be enhanced to track actual recent activity
        # For now, return empty to avoid errors
        return jsonify({
            "recentDownloads": [],
            "recentRuleApplications": []
        })
        
    except Exception as e:
        app.logger.error(f"Error getting recent activity: {str(e)}")
        return jsonify({
            "recentDownloads": [],
            "recentRuleApplications": []
        }), 500

@app.route('/api/series-stats')
def series_stats():
    """Get statistics about series and rules."""
    try:
        config = load_config()
        all_series = get_sonarr_series()
        
        rules_mapping = {}
        for rule_name, details in config['rules'].items():
            for series_id in details.get('series', []):
                rules_mapping[str(series_id)] = rule_name
        
        stats = {
            'total_series': len(all_series),
            'assigned_series': len(rules_mapping),
            'unassigned_series': len(all_series) - len(rules_mapping),
            'total_rules': len(config['rules']),
            'rule_breakdown': {}
        }
        
        for rule_name, details in config['rules'].items():
            stats['rule_breakdown'][rule_name] = len(details.get('series', []))
        
        return jsonify(stats)
        
    except Exception as e:
        app.logger.error(f"Error getting series stats: {str(e)}")
        return jsonify({'error': str(e)}), 500

def cleanup_config_rules():
    """Remove series from rules that no longer exist in Sonarr."""
    try:
        config = load_config()
        existing_series = get_sonarr_series()
        existing_series_ids = set(str(series['id']) for series in existing_series)
        
        changes_made = False
        
        for rule_name, rule_details in config['rules'].items():
            original_count = len(rule_details.get('series', []))
            rule_details['series'] = [
                series_id for series_id in rule_details.get('series', [])
                if series_id in existing_series_ids
            ]
            
            if len(rule_details['series']) != original_count:
                removed_count = original_count - len(rule_details['series'])
                app.logger.info(f"Cleaned up rule '{rule_name}': Removed {removed_count} non-existent series")
                changes_made = True
        
        config['rules'] = {
            rule: details for rule, details in config['rules'].items()
            if details.get('series') or rule == config.get('default_rule', 'full_seasons')
        }
        
        if changes_made:
            save_config(config)
            app.logger.info("Configuration cleanup completed")
            
    except Exception as e:
        app.logger.error(f"Error during config cleanup: {str(e)}")

@app.route('/cleanup')
def cleanup():
    """Manual cleanup of rules."""
    cleanup_config_rules()
    return redirect(url_for('index', message="Configuration cleaned up successfully"))

@app.route('/scheduler')
def scheduler_admin():
    """Scheduler administration page."""
    return render_template('scheduler_admin.html')

@app.route('/api/scheduler-status')
def scheduler_status():
    """Get scheduler status."""
    try:
        if 'cleanup_scheduler' not in globals():
            return jsonify({"status": "error", "message": "Scheduler not initialized"}), 500
        return jsonify(cleanup_scheduler.get_status())
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/force-cleanup', methods=['POST'])
def force_cleanup():
    """Manually trigger cleanup with unified logging."""
    try:
        print("Manual cleanup requested via API")
        result = cleanup_scheduler.force_cleanup()
        print("Manual cleanup started successfully")
        return jsonify({"status": "success", "message": result})
        
    except Exception as e:
        print(f"Failed to start manual cleanup: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

    
@app.errorhandler(404)
def not_found(error):
    return render_template('error.html', message="Page not found"), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('error.html', message="Internal server error"), 500



@app.route('/api/recent-cleanup-activity')
def recent_cleanup_activity():
    """Fixed recent cleanup activity endpoint."""
    return jsonify({
        "recentCleanups": [],
        "totalOperations": 0,
        "status": "success"
    })
    


@app.route('/debug-series/<int:series_id>')
def debug_series(series_id):
    """Debug what cleanup would do for a specific series."""
    try:
        # Import cleanup functions
        from servertosonarr import (
            load_activity_tracking, check_time_based_cleanup, 
            load_config as load_server_config
        )
        
        # Get rule for this series
        config = load_config()
        rule = None
        rule_name = None
        
        for r_name, r_details in config['rules'].items():
            if str(series_id) in r_details.get('series', []):
                rule = r_details
                rule_name = r_name
                break
        
        if not rule:
            return jsonify({"error": f"Series {series_id} not found in any rule"})
        
        # Get activity data
        activity_data = load_activity_tracking()
        series_activity = activity_data.get(str(series_id), {})
        
        # Check cleanup decision
        should_cleanup, reason = check_time_based_cleanup(series_id, rule)
        
        return jsonify({
            "series_id": series_id,
            "rule_name": rule_name,
            "rule_config": rule,
            "activity_data": series_activity,
            "should_cleanup": should_cleanup,
            "cleanup_reason": reason,
            "current_time": int(time.time()),
            "days_since_last_watch": (int(time.time()) - series_activity.get('last_watched', 0)) / (24*60*60) if series_activity.get('last_watched') else "Never watched"
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route('/api/test-cleanup/<int:series_id>')
def test_cleanup(series_id):
    """Test cleanup for a specific series (always in dry run mode)."""
    try:
        config = load_config()
        
        # Find the rule for this series
        rule = None
        rule_name = None
        for r_name, r_details in config['rules'].items():
            if str(series_id) in r_details.get('series', []):
                rule = r_details
                rule_name = r_name
                break
        
        if not rule:
            return jsonify({"status": "error", "message": "Series not assigned to any rule"}), 404
        
        # Force dry run mode for testing
        test_rule = rule.copy()
        test_rule['dry_run'] = True
        
        # Import cleanup functions
        from servertosonarr import check_time_based_cleanup, perform_time_based_cleanup
        
        # Check if cleanup would be performed
        should_cleanup, reason = check_time_based_cleanup(series_id, test_rule)
        
        if should_cleanup:
            # Capture logs for the test (this would need custom log capture)
            perform_time_based_cleanup(series_id, test_rule, reason)
            
            return jsonify({
                "status": "success",
                "message": f"Test completed: {reason}",
                "would_cleanup": True,
                "rule": rule_name,
                "reason": reason
            })
        else:
            return jsonify({
                "status": "success", 
                "message": f"No cleanup needed: {reason}",
                "would_cleanup": False,
                "rule": rule_name,
                "reason": reason
            })
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    
@app.route('/add-test-activity/<int:series_id>', methods=['POST'])
def add_test_activity(series_id):
    """Add test activity data for debugging."""
    try:
        from servertosonarr import load_activity_tracking, save_activity_tracking
        
        days_ago = int(request.form.get('days_ago', 16))  # Default 16 days ago
        season = int(request.form.get('season', 1))
        episode = int(request.form.get('episode', 1))
        
        # Calculate timestamp
        current_time = int(time.time())
        watch_time = current_time - (days_ago * 24 * 60 * 60)
        
        # Load and update activity data
        activity_data = load_activity_tracking()
        activity_data[str(series_id)] = {
            "last_watched": watch_time,
            "last_updated": current_time,
            "last_season": season,
            "last_episode": episode
            
        }
        
        save_activity_tracking(activity_data)
        
        return jsonify({
            "status": "success",
            "message": f"Added activity for series {series_id}: watched S{season}E{episode} {days_ago} days ago",
            "watch_timestamp": watch_time,
            "days_ago": days_ago
        })
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500



def get_cleanup_summary_fallback():
    """Fallback cleanup summary when the main function isn't available."""
    try:
        CLEANUP_LOG_PATH = os.getenv('CLEANUP_LOG_PATH', '/app/logs/cleanup.log')
        
        if not os.path.exists(CLEANUP_LOG_PATH):
            return {"recent_cleanups": [], "total_operations": 0}
        
        recent_cleanups = []
        
        # Read the last 50 lines of the cleanup log
        try:
            with open(CLEANUP_LOG_PATH, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                
            # Look for cleanup completion messages in the last 50 lines
            for line in reversed(lines[-50:]):
                if "CLEANUP COMPLETED" in line:
                    try:
                        timestamp = line.split(' - ')[0]
                        recent_cleanups.append({
                            "timestamp": timestamp,
                            "type": "Scheduled" if "Scheduled" in line else "Manual",
                            "status": "completed"
                        })
                        
                        if len(recent_cleanups) >= 3:  # Limit to 3 recent cleanups
                            break
                    except:
                        continue
        except Exception as e:
            current_app.logger.debug(f"Could not read cleanup log: {str(e)}")
        
        return {
            "recent_cleanups": recent_cleanups,
            "total_operations": len(recent_cleanups)
        }
        
    except Exception as e:
        current_app.logger.error(f"Error in cleanup summary fallback: {str(e)}")
        return {"recent_cleanups": [], "total_operations": 0, "error": str(e)}


# Add cleanup summary endpoint for the web UI
def get_cleanup_summary():
    """Get summary of recent cleanup operations for the web interface."""
    try:
        CLEANUP_LOG_PATH = os.getenv('CLEANUP_LOG_PATH', '/app/logs/cleanup.log')
        
        if not os.path.exists(CLEANUP_LOG_PATH):
            return {"recent_cleanups": [], "total_operations": 0}
        
        recent_cleanups = []
        
        # Read the last 100 lines of the cleanup log
        with open(CLEANUP_LOG_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        # Look for cleanup completion messages
        for line in reversed(lines[-100:]):
            if "CLEANUP COMPLETED" in line:
                try:
                    timestamp = line.split(' - ')[0]
                    recent_cleanups.append({
                        "timestamp": timestamp,
                        "type": "Scheduled" if "Scheduled" in line else "Manual",
                        "status": "completed"
                    })
                    
                    if len(recent_cleanups) >= 5:  # Limit to 5 recent cleanups
                        break
                except:
                    continue
        
        return {
            "recent_cleanups": recent_cleanups,
            "total_operations": len(recent_cleanups)
        }
        
    except Exception as e:
        cleanup_logger.error(f"Error getting cleanup summary: {str(e)}")
        return {"recent_cleanups": [], "total_operations": 0, "error": str(e)}
    


@app.route('/api/safety-status')
def safety_status():
    """Get current safety status including dry run settings - FIXED."""
    try:
        config = load_config()
        
        # Check global dry run setting
        global_dry_run = os.getenv('CLEANUP_DRY_RUN', 'false').lower() == 'true'
        
        # Check which rules have dry run enabled
        rules_with_dry_run = []
        for rule_name, rule_details in config.get('rules', {}).items():
            if rule_details.get('dry_run', False):
                rules_with_dry_run.append(rule_name.replace('_', ' ').title())
        
        return jsonify({
            "global_dry_run": global_dry_run,
            "rules_with_dry_run": rules_with_dry_run,
            "total_rules": len(config.get('rules', {})),
            "status": "success"
        })
        
    except Exception as e:
        current_app.logger.error(f"Error getting safety status: {str(e)}")
        return jsonify({
            "global_dry_run": False,
            "rules_with_dry_run": [],
            "total_rules": 0,
            "status": "error", 
            "message": str(e)
        }), 500

@app.route('/cleanup-logs')
def cleanup_logs():
    """Display recent cleanup logs - with fallback for missing template."""
    try:
        CLEANUP_LOG_PATH = os.getenv('CLEANUP_LOG_PATH', '/app/logs/cleanup.log')
        
        if not os.path.exists(CLEANUP_LOG_PATH):
            # Return a simple HTML response instead of template
            return render_simple_logs_page("No cleanup logs found yet.")
        
        # Read the last 200 lines of the cleanup log
        try:
            with open(CLEANUP_LOG_PATH, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            return render_simple_logs_page(f"Error reading log file: {str(e)}")
        
        # Get the last 200 lines and reverse to show newest first
        recent_lines = lines[-200:] if len(lines) > 200 else lines
        recent_lines.reverse()
        
        # Try to render template, fallback to simple HTML if template missing
        try:
            return render_template('cleanup_logs.html', logs=recent_lines)
        except:
            # Template not found, render simple HTML page
            return render_simple_logs_page(recent_lines)
        
    except Exception as e:
        current_app.logger.error(f"Error in cleanup logs route: {str(e)}")
        return render_simple_logs_page(f"Error loading logs: {str(e)}")

def render_simple_logs_page(logs_or_message):
    """Render a simple HTML page for logs when template is missing."""
    if isinstance(logs_or_message, str):
        # It's an error message
        content = f'<div class="alert alert-warning">{logs_or_message}</div>'
    else:
        # It's a list of log lines
        content = '<div style="font-family: monospace; font-size: 0.9em; max-height: 600px; overflow-y: auto; border: 1px solid #333; padding: 1rem; background: #1a1a1a;">'
        for line in logs_or_message:
            line_class = ""
            if 'ERROR' in line or 'Failed' in line:
                line_class = 'style="color: #ff6b6b;"'
            elif 'DRY RUN' in line:
                line_class = 'style="color: #74c0fc;"'
            elif 'CLEANUP STARTED' in line or 'CLEANUP COMPLETED' in line:
                line_class = 'style="color: #51cf66;"'
            elif 'deleted' in line and 'would delete' not in line:
                line_class = 'style="color: #ffd43b;"'
            elif 'SKIPPED' in line:
                line_class = 'style="color: #868e96;"'
            else:
                line_class = 'style="color: #f8f9fa;"'
            
            content += f'<div {line_class}>{line.strip()}</div>'
        content += '</div>'
    
    # Simple HTML page
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Cleanup Logs - OCDarr</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background-color: #1a1a1a; color: #e0e0e0; }}
            .navbar {{ background-color: #2d2d2d !important; }}
            .card {{ background-color: #2d2d2d; border: 1px solid #404040; }}
        </style>
    </head>
    <body>
        <nav class="navbar navbar-expand-lg navbar-dark">
            <div class="container">
                <a class="navbar-brand" href="/">OCDarr</a>
            </div>
        </nav>
        <div class="container mt-4">
            <div class="card">
                <div class="card-header d-flex justify-content-between">
                    <h5><i class="fas fa-file-alt me-2"></i>Cleanup Logs</h5>
                    <a href="/scheduler" class="btn btn-primary btn-sm">Back to Scheduler</a>
                </div>
                <div class="card-body">
                    {content}
                </div>
            </div>
        </div>
    </body>
    </html>
    '''
    return html

@app.route('/dry-run-settings', methods=['GET', 'POST'])
def dry_run_settings():
    """Manage dry run settings for rules - FIXED."""
    if request.method == 'POST':
        try:
            app.logger.info(f"Form data received: {dict(request.form)}")
           
            config = load_config()
            app.logger.info(f"Config before changes: {config['rules']['nukeafter90days'].get('dry_run', 'NOT_SET')}")
           
            # Update rule-specific dry run settings
            for rule_name in config.get('rules', {}).keys():
                rule_dry_run_key = f'rule_dry_run_{rule_name}'
                rule_dry_run = rule_dry_run_key in request.form  # ONLY CHANGE THIS LINE
               
                app.logger.info(f"Setting {rule_name} dry_run to: {rule_dry_run}")
                config['rules'][rule_name]['dry_run'] = rule_dry_run
           
            app.logger.info(f"Config after changes: {config['rules']['nukeafter90days'].get('dry_run', 'NOT_SET')}")
           
            save_config(config)
            app.logger.info("save_config() called")
           
            # VERIFY IT WAS SAVED
            verify_config = load_config()
            app.logger.info(f"Config after save_config: {verify_config['rules']['nukeafter90days'].get('dry_run', 'NOT_SET')}")
               
            return redirect(url_for('scheduler_admin', message="Dry run settings saved successfully"))
        except Exception as e:
            current_app.logger.error(f"Error saving dry run settings: {str(e)}")
            return redirect(url_for('scheduler_admin', message=f"Error saving settings: {str(e)}"))
   
    # GET request - show current settings  
    try:
        config = load_config()
        global_dry_run = os.getenv('CLEANUP_DRY_RUN', 'false').lower() == 'true'
       
        return render_template('dry_run_settings.html',
                             config=config,
                             global_dry_run=global_dry_run)
           
    except Exception as e:
        current_app.logger.error(f"Error loading dry run settings: {str(e)}")
        return redirect(url_for('scheduler_admin', message=f"Error loading settings: {str(e)}"))


def render_simple_dry_run_page(config, global_dry_run):
    """Simple fallback for dry run settings when template is missing."""
    
    rules_html = ""
    for rule_name, rule_details in config.get('rules', {}).items():
        checked = 'checked' if rule_details.get('dry_run', False) else ''
        series_count = len(rule_details.get('series', []))
        rules_html += f'''
        <div class="mb-3 p-3 border rounded">
            <div class="form-check">
                <input class="form-check-input" type="checkbox" name="rule_dry_run_{rule_name}" {checked}>
                <label class="form-check-label">
                    <strong>{rule_name.replace('_', ' ').title()}</strong> ({series_count} series)
                </label>
            </div>
        </div>
        '''
    
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Dry Run Settings - OCDarr</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background-color: #1a1a1a; color: #e0e0e0; }}
            .card {{ background-color: #2d2d2d; border: 1px solid #404040; }}
            .form-control, .form-check-input {{ background-color: #404040; border-color: #606060; }}
        </style>
    </head>
    <body>
        <div class="container mt-4">
            <div class="card">
                <div class="card-header">
                    <h5>Dry Run Settings</h5>
                </div>
                <div class="card-body">
                    <form method="POST">
                        <div class="alert alert-info">
                            <strong>Global Dry Run:</strong> {'ENABLED' if global_dry_run else 'DISABLED'}<br>
                            <small>Change via CLEANUP_DRY_RUN environment variable</small>
                        </div>
                        
                        <h6>Rule-Specific Settings:</h6>
                        {rules_html}
                        
                        <div class="mt-3">
                            <button type="submit" class="btn btn-primary">Save Settings</button>
                            <a href="/scheduler" class="btn btn-secondary">Back to Scheduler</a>
                        </div>
                    </form>
                </div>
            </div>
        </div>
    </body>
    </html>
    '''
    return html
# =============================================================================
# WEBHOOK ROUTES - ACTIVITY TRACKING ONLY, NO DELETIONS
# =============================================================================

@app.route('/sonarr-webhook', methods=['POST'])
def process_sonarr_webhook():
    """Handle incoming Sonarr webhooks for series additions - NO DELETIONS."""
    app.logger.info("Received webhook from Sonarr")
    
    try:
        json_data = request.json
        
        # Check if this is a "SeriesAdd" event
        event_type = json_data.get('eventType')
        if event_type != 'SeriesAdd':
            return jsonify({"message": "Not a series add event"}), 200
            
        # Get important data from the webhook
        series = json_data.get('series', {})
        series_id = series.get('id')
        tvdb_id = series.get('tvdbId')
        tmdb_id = series.get('tmdbId')
        series_title = series.get('title')
        
        app.logger.info(f"Processing series addition: {series_title} (ID: {series_id}, TVDB: {tvdb_id})")
        
        # Setup Sonarr connection
        sonarr_preferences = sonarr_utils.load_preferences()
        headers = {
            'X-Api-Key': sonarr_preferences['SONARR_API_KEY'],
            'Content-Type': 'application/json'
        }
        sonarr_url = sonarr_preferences['SONARR_URL']

        # First, get all tags from Sonarr
        tags_response = requests.get(f"{sonarr_url}/api/v3/tag", headers=headers)
        tags = tags_response.json()

        # Create a mapping of tag IDs to tag labels
        tag_mapping = {tag['id']: tag['label'] for tag in tags}

        # Check series tags
        series_tags = series.get('tags', [])
        app.logger.info(f"Series tags: {series_tags}")
        app.logger.info(f"Tag mapping: {tag_mapping}")

        # Check if any of the tags match the 'ocdarr' label
        # Handle both tag IDs (numbers) and tag names (strings) from webhook
        has_ocdarr_tag = False
        for tag in series_tags:
            if isinstance(tag, int):
                # Tag is an ID, look it up in mapping
                if tag_mapping.get(tag, '').lower() == 'ocdarr':
                    has_ocdarr_tag = True
                    break
            else:
                # Tag is already a name/string
                if str(tag).lower() == 'ocdarr':
                    has_ocdarr_tag = True
                    break
        
        # If no ocdarr tag, just exit
        if not has_ocdarr_tag:
            app.logger.info(f"Series {series_title} has no ocdarr tag, skipping processing")
            
            return jsonify({
                "status": "success",
                "message": "Series has no ocdarr tag, no processing needed"
            }), 200
        
        # If it has ocdarr tag, proceed with processing
        app.logger.info(f"Series {series_title} has ocdarr tag, proceeding with processing")
        global jellyseerr_pending_requests
        app.logger.info(f"Looking for TVDB ID {tvdb_id} in pending requests")

        tvdb_id_str = str(tvdb_id)
        if tvdb_id_str in jellyseerr_pending_requests:
            jellyseerr_request = jellyseerr_pending_requests[tvdb_id_str]
            app.logger.info(f"Found matching Jellyseerr request for {series_title}: {jellyseerr_request}")
        
            # Delete the Jellyseerr request if it exists
            if jellyseerr_request and 'request_id' in jellyseerr_request:
                request_id = jellyseerr_request['request_id']
                app.logger.info(f"Canceling Jellyseerr request {request_id} for {series_title}")
            
                # Direct cancellation
                result = modified_episeerr.delete_overseerr_request(request_id)
                app.logger.info(f"Jellyseerr cancellation result: {result}")
            
                # Remove from pending requests
                del jellyseerr_pending_requests[tvdb_id_str]
                app.logger.info(f"Removed request {request_id} from pending requests dictionary")
        else:
            app.logger.info(f"No matching Jellyseerr request found for TVDB ID: {tvdb_id_str}")
            
        # Check if a request already exists for this series
        existing_request = None
        for filename in os.listdir(REQUESTS_DIR):
            if filename.endswith('.json'):
                try:
                    with open(os.path.join(REQUESTS_DIR, filename), 'r') as f:
                        request_data = json.load(f)
                        # Check if this is a request for the same series
                        if (request_data.get('series_id') == series_id or 
                            (tmdb_id and request_data.get('tmdb_id') == tmdb_id) or 
                            (tvdb_id and request_data.get('tvdb_id') == tvdb_id)):
                            existing_request = request_data
                            app.logger.info(f"Found existing request for {series_title}")
                            break
                except Exception as e:
                    app.logger.error(f"Error reading request file {filename}: {str(e)}")
        
        # If a request already exists, don't create a new one
        if existing_request:
            app.logger.info(f"Using existing request for {series_title}")
            return jsonify({
                "status": "success",
                "message": "Request already exists for this series"
            }), 200
        
        # 1. Unmonitor ALL episodes
        try:
            # Get all episodes for the series
            episodes_response = requests.get(
                f"{sonarr_url}/api/v3/episode?seriesId={series_id}",
                headers=headers
            )
            
            if episodes_response.ok and episodes_response.json():
                all_episodes = episodes_response.json()
                all_episode_ids = [episode["id"] for episode in all_episodes]
                
                if all_episode_ids:
                    unmonitor_response = requests.put(
                        f"{sonarr_url}/api/v3/episode/monitor",
                        headers=headers,
                        json={"episodeIds": all_episode_ids, "monitored": False}
                    )
                    
                    if unmonitor_response.ok:
                        app.logger.info(f"Unmonitored all episodes for series {series_title}")
                    else:
                        app.logger.error(f"Failed to unmonitor episodes: {unmonitor_response.text}")
        except Exception as e:
            app.logger.error(f"Error unmonitoring episodes: {str(e)}")
        
        # 2. Cancel any active downloads
        try:
            modified_episeerr.check_and_cancel_unmonitored_downloads()
        except Exception as e:
            app.logger.error(f"Error cancelling downloads: {str(e)}")
        
        # 3. Add to default rule
        config = load_config()
        default_rule_name = config.get('default_rule', 'Default')

        if default_rule_name in config['rules']:
            series_id_str = str(series_id)
            
            # NEW DICT STRUCTURE
            series_dict = config['rules'][default_rule_name].get('series', {})
            if series_id_str not in series_dict:
                series_dict[series_id_str] = {'activity_date': None}
                config['rules'][default_rule_name]['series'] = series_dict
                save_config(config)
                app.logger.info(f"Added series {series_title} (ID: {series_id}) to default rule")

        # 4. Execute the default rule immediately for new series (NO DELETIONS)
        try:
            rule_config = config['rules'][default_rule_name]
            get_option = rule_config.get('get_option')
            action_option = rule_config.get('action_option', 'monitor')
            
            app.logger.info(f"Executing default rule '{default_rule_name}' with get_option '{get_option}' for new series {series_title}")
            
            # Get all episodes for the series (we already have this from step 1, but get fresh data)
            episodes_response = requests.get(
                f"{sonarr_url}/api/v3/episode?seriesId={series_id}",
                headers=headers
            )
            
            if episodes_response.ok:
                all_episodes = episodes_response.json()
                
                # Get Season 1 episodes, sorted by episode number
                season1_episodes = sorted(
                    [ep for ep in all_episodes if ep.get('seasonNumber') == 1],
                    key=lambda x: x.get('episodeNumber', 0)
                )
                
                if not season1_episodes:
                    app.logger.warning(f"No Season 1 episodes found for {series_title}")
                else:
                    # Determine which episodes to monitor based on get_option
                    episodes_to_monitor = []
                    
                    if get_option == 'all':
                        # Get all episodes from all seasons
                        episodes_to_monitor = [ep['id'] for ep in all_episodes]
                        app.logger.info(f"Getting all episodes for {series_title}")
                        
                    elif get_option == 'season':
                        # Get all episodes from Season 1
                        episodes_to_monitor = [ep['id'] for ep in season1_episodes]
                        app.logger.info(f"Getting Season 1 ({len(episodes_to_monitor)} episodes) for {series_title}")
                        
                    else:
                        try:
                            # Treat as number of episodes to get from Season 1
                            num_episodes = int(get_option)
                            episodes_to_monitor = [ep['id'] for ep in season1_episodes[:num_episodes]]
                            app.logger.info(f"Getting first {len(episodes_to_monitor)} episodes for {series_title}")
                        except (ValueError, TypeError):
                            # Fallback to pilot episode if get_option is invalid
                            episodes_to_monitor = [season1_episodes[0]['id']] if season1_episodes else []
                            app.logger.warning(f"Invalid get_option '{get_option}', defaulting to pilot episode for {series_title}")
                    
                    if episodes_to_monitor:
                        # Monitor the selected episodes
                        monitor_response = requests.put(
                            f"{sonarr_url}/api/v3/episode/monitor",
                            headers=headers,
                            json={"episodeIds": episodes_to_monitor, "monitored": True}
                        )
                        
                        if monitor_response.ok:
                            app.logger.info(f"Monitored {len(episodes_to_monitor)} episodes for {series_title}")
                            
                            # Search for episodes if action_option is 'search'
                            if action_option == 'search':
                                search_response = requests.post(
                                    f"{sonarr_url}/api/v3/command",
                                    headers=headers,
                                    json={"name": "EpisodeSearch", "episodeIds": episodes_to_monitor}
                                )
                                
                                if search_response.ok:
                                    app.logger.info(f"Started search for {len(episodes_to_monitor)} episodes of {series_title}")
                                else:
                                    app.logger.error(f"Failed to search for episodes: {search_response.text}")
                        else:
                            app.logger.error(f"Failed to monitor episodes: {monitor_response.text}")
                    else:
                        app.logger.warning(f"No episodes to monitor for {series_title}")
            else:
                app.logger.error(f"Failed to get episodes for series: {episodes_response.text}")
        except Exception as e:
            app.logger.error(f"Error executing default rule for new series: {str(e)}", exc_info=True)
        
        # 5. Remove ocdarr tag after successful rule execution
        try:
            # Get the current series info to check tags
            series_response = requests.get(f"{sonarr_url}/api/v3/series/{series_id}", headers=headers)
            if series_response.ok:
                current_series = series_response.json()
                
                # Check if ocdarr tag exists and remove it
                if modified_episeerr.OCDARR_TAG_ID in current_series.get('tags', []):
                    updated_tags = [tag for tag in current_series.get('tags', []) if tag != modified_episeerr.OCDARR_TAG_ID]
                    
                    update_payload = current_series.copy()
                    update_payload['tags'] = updated_tags
                    
                    update_response = requests.put(f"{sonarr_url}/api/v3/series", headers=headers, json=update_payload)
                    if update_response.ok:
                        app.logger.info(f"Removed ocdarr tag from series {series_title} (ID: {series_id}) after rule execution")
                    else:
                        app.logger.error(f"Failed to remove ocdarr tag: {update_response.text}")
                else:
                    app.logger.info(f"Series {series_title} does not have ocdarr tag to remove")
            else:
                app.logger.error(f"Failed to get series info for tag removal: {series_response.text}")
        except Exception as tag_error:
            app.logger.error(f"Error removing ocdarr tag: {str(tag_error)}")

        return jsonify({
            "status": "success",
            "message": "Series processed and added to default rule"
        }), 200
            
    except Exception as e:
        app.logger.error(f"Error processing Sonarr webhook: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/seerr-webhook', methods=['POST'])
def process_seerr_webhook():
    """Handle incoming Jellyseerr webhooks - store request info for later."""
    try:
        app.logger.info("Received webhook from Jellyseerr")
        json_data = request.json
        
        # Debug log the webhook data
        app.logger.info(f"Jellyseerr webhook data: {json.dumps(json_data)}")
        
        # Get the request ID
        request_id = json_data.get('request', {}).get('request_id') or json_data.get('request', {}).get('id')
        
        # Check if it's a TV show request
        media_type = json_data.get('media', {}).get('media_type')
        if media_type != 'tv':
            app.logger.info(f"Request is not a TV show request. Skipping.")
            return jsonify({"status": "success"}), 200
        
        # Store the TVDB ID, request ID, and title in the global dictionary
        tvdb_id = json_data.get('media', {}).get('tvdbId')
        title = json_data.get('subject', 'Unknown Show')
        
        if tvdb_id and request_id:
            # Store the request info for later use by the Sonarr webhook
            global jellyseerr_pending_requests
            jellyseerr_pending_requests[str(tvdb_id)] = {
                'request_id': request_id,
                'title': title,
                'timestamp': int(time.time())
            }
            
            app.logger.info(f"Stored Jellyseerr request {request_id} for TVDB ID {tvdb_id} ({title})")
            
            # Clean up old requests (older than 10 minutes)
            current_time = int(time.time())
            expired_tvdb_ids = []
            
            for tid, info in jellyseerr_pending_requests.items():
                if current_time - info.get('timestamp', 0) > 600:  # 10 minutes
                    expired_tvdb_ids.append(tid)
            
            for tid in expired_tvdb_ids:
                del jellyseerr_pending_requests[tid]
                app.logger.info(f"Cleaned up expired request for TVDB ID {tid}")
        
        return jsonify({"status": "success"}), 200
        
    except Exception as e:
        app.logger.error(f"Error processing Jellyseerr webhook: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/webhook', methods=['POST'])
def handle_server_webhook():
    """Handle webhooks from Plex/Tautulli - ACTIVITY TRACKING ONLY."""
    app.logger.info("Received webhook from Tautulli")
    data = request.json
    if data:
        try:
            temp_dir = os.path.join(os.getcwd(), 'temp')
            os.makedirs(temp_dir, exist_ok=True)
            
            # Standardize field names for Plex/Tautulli data
            plex_data = {
                "server_title": data.get('plex_title'),
                "server_season_num": data.get('plex_season_num'),
                "server_ep_num": data.get('plex_ep_num')
            }
            
            # Save to the standardized filename
            with open(os.path.join(temp_dir, 'data_from_server.json'), 'w') as f:
                json.dump(plex_data, f)
            
            # Call servertosonarr.py which now ONLY handles activity tracking and next content
            # NO DELETIONS happen in webhook processing
            result = subprocess.run(["python3", os.path.join(os.getcwd(), "servertosonarr.py")], capture_output=True, text=True)
            if result.stderr:
                app.logger.error(f"Servertosonarr.py error: {result.stderr}")
            
            app.logger.info("Webhook processing completed - activity tracked, next content processed")
            return jsonify({'status': 'success'}), 200
        except Exception as e:
            app.logger.error(f"Failed to process Tautulli webhook: {str(e)}")
            return jsonify({'status': 'error', 'message': str(e)}), 500
    return jsonify({'status': 'error', 'message': 'No data received'}), 400

@app.route('/jellyfin-webhook', methods=['POST'])
def handle_jellyfin_webhook():
    """Handle webhooks from Jellyfin for playback progress - ACTIVITY TRACKING ONLY."""
    app.logger.info("Received webhook from Jellyfin")
    data = request.json
    if not data:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400

    try:
        if data.get('NotificationType') == 'PlaybackProgress':
            position_ticks = int(data.get('PlaybackPositionTicks', 0))
            total_ticks = int(data.get('RunTimeTicks', 0))
            
            if total_ticks > 0:
                progress_percent = (position_ticks / total_ticks) * 100
                app.logger.info(f"Jellyfin playback progress: {progress_percent:.2f}%")
                
                # Only process when progress is between 45-55% (mid-episode)
                if 45 <= progress_percent <= 55:
                    item_type = data.get('ItemType')
                    
                    # Only process for TV episodes
                    if item_type == 'Episode':
                        series_name = data.get('SeriesName')
                        season = data.get('SeasonNumber')
                        episode = data.get('EpisodeNumber')
                        
                        if all([series_name, season is not None, episode is not None]):
                            app.logger.info(f"Processing Jellyfin episode: {series_name} S{season}E{episode}")
                            
                            # Format data using server prefix instead of plex
                            jellyfin_data = {
                                "server_title": series_name,
                                "server_season_num": str(season),
                                "server_ep_num": str(episode)
                            }
                            
                            # Save to the standardized file name
                            temp_dir = os.path.join(os.getcwd(), 'temp')
                            os.makedirs(temp_dir, exist_ok=True)
                            with open(os.path.join(temp_dir, 'data_from_server.json'), 'w') as f:
                                json.dump(jellyfin_data, f)
                            
                            # Call servertosonarr.py which now ONLY handles activity tracking and next content
                            # NO DELETIONS happen in webhook processing
                            result = subprocess.run(["python3", os.path.join(os.getcwd(), "servertosonarr.py")], 
                                                   capture_output=True, text=True)
                            
                            if result.stderr:
                                app.logger.error(f"Errors from servertosonarr.py: {result.stderr}")
                            
                            app.logger.info("Jellyfin webhook processing completed - activity tracked, next content processed")
                        else:
                            app.logger.warning(f"Missing episode info: Series={series_name}, Season={season}, Episode={episode}")
                    else:
                        app.logger.info(f"Item type '{item_type}' is not an episode, ignoring")
                else:
                    app.logger.debug(f"Progress {progress_percent:.2f}% outside trigger range (45-55%), ignoring")
            else:
                app.logger.warning("Total ticks is zero, cannot calculate progress")
        else:
            notification_type = data.get('NotificationType', 'Unknown')
            app.logger.info(f"Jellyfin notification type '{notification_type}' is not PlaybackProgress, ignoring")
            
        return jsonify({'status': 'success'}), 200
            
    except Exception as e:
        app.logger.error(f"Failed to process Jellyfin webhook: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500

def initialize_episeerr():
    """Initialize episode tag and check for unmonitored downloads."""
    modified_episeerr.create_ocdarr_tag()
    app.logger.info("Created ocdarr tag")
    
    # Do an initial check for unmonitored downloads
    try:
        modified_episeerr.check_and_cancel_unmonitored_downloads()
    except Exception as e:
        app.logger.error(f"Error in initial download check: {str(e)}")

# NOW CREATE THE INSTANCE (AFTER THE CLASS IS DEFINED)
cleanup_scheduler = OCDarrScheduler()
app.logger.info("✓ OCDarrScheduler instantiated successfully")
cleanup_scheduler.start_scheduler()  # Just start it directly

if __name__ == '__main__':
    # Call config rules cleanup at startup
    cleanup_config_rules()
    
    # Call initialization function before running the app
    initialize_episeerr()
    
    app.logger.info("🚀 OCDarr webhook listener starting - webhook handles activity tracking only, scheduler handles all deletions")
    
    # Start the Flask application
    app.run(host='0.0.0.0', port=5002, debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true')
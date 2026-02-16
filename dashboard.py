"""
Episeerr Dashboard Module
Unified media dashboard with calendar, stats, and activity feed
"""

from flask import Blueprint, render_template, jsonify
import requests
import os
import json
from datetime import datetime, timedelta
import logging
from integrations import get_all_integrations

dashboard_bp = Blueprint('dashboard', __name__)
from logging_config import main_logger as logger

# Database-first configuration helpers
def get_sonarr_config():
    """Get Sonarr config from database or env"""
    from settings_db import get_service
    from episeerr_utils import normalize_url
    
    service = get_service('sonarr', 'default')
    if service:
        return normalize_url(service['url']), service['api_key']
    # Fallback to env
    return os.getenv('SONARR_URL'), os.getenv('SONARR_API_KEY')

def get_jellyfin_config():
    """Get Jellyfin config from database or env"""
    from settings_db import get_service
    from episeerr_utils import normalize_url
    
    service = get_service('jellyfin', 'default')
    if service:
        return normalize_url(service['url']), service['api_key']
    # Fallback to env
    return os.getenv('JELLYFIN_URL'), os.getenv('JELLYFIN_API_KEY')

def get_tautulli_config():
    """Get Tautulli config from database or env"""
    from settings_db import get_service
    from episeerr_utils import normalize_url
    
    service = get_service('tautulli', 'default')
    if service:
        return normalize_url(service['url']), service['api_key']
    # Fallback to env
    return os.getenv('TAUTULLI_URL'), os.getenv('TAUTULLI_API_KEY')

# Load configurations
SONARR_URL, SONARR_API_KEY = get_sonarr_config()
JELLYFIN_URL, JELLYFIN_API_KEY = get_jellyfin_config()
TAUTULLI_URL, TAUTULLI_API_KEY = get_tautulli_config()


def get_series_banner(series_id):
    """Get banner URL from Sonarr for series"""
    try:
        headers = {'X-Api-Key': SONARR_API_KEY}
        response = requests.get(f"{SONARR_URL}/api/v3/series/{series_id}", headers=headers, timeout=5)
        
        if response.ok:
            series_data = response.json()
            for image in series_data.get('images', []):
                if image.get('coverType') == 'banner':
                    return image.get('remoteUrl')
        return None
    except:
        return None



@dashboard_bp.route('/dashboard')
def dashboard():
    """Main dashboard page"""
    return render_template('dashboard.html')


@dashboard_bp.route('/api/dashboard/calendar')
def calendar_data():
    """Get upcoming episodes + recent downloads (two separate lists)"""
    try:
        from datetime import datetime, timedelta
        import os
        import json
        
        # Check if Sonarr is configured
        if not SONARR_URL or not SONARR_API_KEY:
            return jsonify({
                'success': False,
                'error': 'Sonarr not configured',
                'message': 'Please configure Sonarr in the setup page',
                'configured': False,
                'upcoming': [],
                'downloaded': []
            })
        
        today = datetime.now()
        week_ahead = today + timedelta(days=7)
        
        logger.info(f"Calendar range: {today.strftime('%Y-%m-%d')} to {week_ahead.strftime('%Y-%m-%d')}")
        
        # ──────────────────────────────────────────────────────
        # 1. GET UPCOMING FROM SONARR (next 7 days)
        # ──────────────────────────────────────────────────────
        headers = {'X-Api-Key': SONARR_API_KEY}
        calendar_url = f"{SONARR_URL}/api/v3/calendar"
        params = {
            'start': today.strftime('%Y-%m-%d'),
            'end': week_ahead.strftime('%Y-%m-%d'),
            'includeSeries': 'true',
            'includeUnmonitored': 'false'
        }
        
        response = requests.get(calendar_url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        upcoming_episodes = response.json()
        
        logger.info(f"Sonarr returned {len(upcoming_episodes)} upcoming episodes")
        
        # ──────────────────────────────────────────────────────
        # 2. GET RECENT DOWNLOADS (last 7 days)
        # ──────────────────────────────────────────────────────
        recent_downloads = []
        downloads_file = os.path.join(os.getcwd(), 'data', 'recent_downloads.json')
        
        if os.path.exists(downloads_file):
            try:
                with open(downloads_file, 'r') as f:
                    recent_downloads = json.load(f)
                logger.info(f"Loaded {len(recent_downloads)} recent downloads")
            except Exception as e:
                logger.error(f"Error loading downloads: {e}")
        
        # ──────────────────────────────────────────────────────
        # 2.5 LOAD WATCHED EPISODES TO FILTER OUT
        # ──────────────────────────────────────────────────────
        watched_episodes = set()
        watched_file = os.path.join(os.getcwd(), 'data', 'activity', 'watched.json')
        
        if os.path.exists(watched_file):
            try:
                with open(watched_file, 'r') as f:
                    watched_data = json.load(f)
                    for watch in watched_data:
                        watched_episodes.add((
                            watch.get('series_id'),
                            watch.get('season'),
                            watch.get('episode')
                        ))
                logger.info(f"Loaded {len(watched_episodes)} watched episodes to filter")
            except Exception as e:
                logger.error(f"Error loading watched episodes: {e}")
        
               
        # ──────────────────────────────────────────────────────
        # 3. LOAD EPISEERR CONFIG FOR RULES
        # ──────────────────────────────────────────────────────
        from episeerr import load_config
        config = load_config()
        
        series_rules = {}
        for rule_name, rule_data in config.get('rules', {}).items():
            for series_id in rule_data.get('series', {}).keys():
                series_rules[int(series_id)] = rule_name
        
        # ──────────────────────────────────────────────────────
        # 4. PROCESS UPCOMING EPISODES
        # ──────────────────────────────────────────────────────
        upcoming_events = []
        now = datetime.now()
        downloaded_ids = {(dl['series_id'], dl['season'], dl['episode']) for dl in recent_downloads}
        
        for ep in upcoming_episodes:
            series_id = ep.get('seriesId')
            season = ep.get('seasonNumber')
            episode = ep.get('episodeNumber')
            
            # Skip if already in downloaded list
            if (series_id, season, episode) in downloaded_ids:
                continue
            
            has_rule = series_id in series_rules
            rule_name = series_rules.get(series_id)
            has_file = ep.get('hasFile', False)
            monitored = ep.get('monitored', False)
            
            air_date_str = ep.get('airDateUtc', '')
            has_aired = False
            if air_date_str:
                try:
                    air_date = datetime.fromisoformat(air_date_str.replace('Z', ''))
                    has_aired = air_date < now
                except:
                    has_aired = False
            
            # Determine status
            if has_file:
                status = 'downloaded'
                color = 'gray'
            elif not monitored:
                status = 'unmonitored'
                color = 'muted'
            elif has_rule:
                status = 'has_rule'
                color = 'green'
            elif has_aired and not has_file:
                status = 'not_grabbed'
                color = 'blue'
            else:
                status = 'no_rule'
                color = 'yellow'
            
            upcoming_events.append({
                'series_id': series_id,
                'series_title': ep.get('series', {}).get('title', 'Unknown'),
                'episode_title': ep.get('title', 'TBA'),
                'season': season,
                'episode': episode,
                'air_date': air_date_str,
                'has_rule': has_rule,
                'rule_name': rule_name,
                'status': status,
                'color': color,
                'banner': get_series_banner(series_id)  # ADD THIS LINE
            })
        
        # ──────────────────────────────────────────────────────
        # 5. FORMAT RECENT DOWNLOADS (use grab timestamp)
        # ──────────────────────────────────────────────────────
        downloaded_events = []
        
        for dl in recent_downloads:
            # Skip if already watched
            dl_key = (dl['series_id'], dl['season'], dl['episode'])
            if dl_key in watched_episodes:
                continue
            
            has_rule = dl['series_id'] in series_rules
            
            downloaded_events.append({
                'series_id': dl['series_id'],
                'series_title': dl['series_title'],
                'episode_title': dl.get('episode_title', ''),
                'season': dl['season'],
                'episode': dl['episode'],
                'grabbed_date': dl['timestamp'],
                'has_rule': has_rule,
                'rule_name': series_rules.get(dl['series_id']),
                'status': 'ready',
                'color': 'green',
                'banner': get_series_banner(dl['series_id'])  # ADD THIS LINE
            })
        # Sort by grab time (newest first)
        downloaded_events.sort(key=lambda x: x['grabbed_date'], reverse=True)
        
        return jsonify({
            'success': True,
            'upcoming': upcoming_events,
            'downloaded': downloaded_events,
            'upcoming_count': len(upcoming_events),
            'downloaded_count': len(downloaded_events)
        })
        
    except Exception as e:
        logger.error(f"Error fetching calendar data: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e),
            'upcoming': [],
            'downloaded': []
        }), 500

@dashboard_bp.route('/api/dashboard/integrations')
def dashboard_integrations():
    """Get metadata for all integrations (for auto-generating UI)"""
    from integrations import get_all_integrations
    
    integrations_data = []
    for integration in get_all_integrations():
        integrations_data.append({
            'service_name': integration.service_name,
            'display_name': integration.display_name,
            'icon': integration.icon,
            'widget': integration.get_dashboard_widget()
        })
    
    return jsonify({
        'success': True,
        'integrations': integrations_data
    })

@dashboard_bp.route('/api/dashboard/stats')
def dashboard_stats():
    """Get overall statistics for dashboard"""
    try:
        stats = {}
        
        # Sonarr stats
        if SONARR_URL and SONARR_API_KEY:
            try:
                headers = {'X-Api-Key': SONARR_API_KEY}
                
                # Get series count
                series_response = requests.get(f"{SONARR_URL}/api/v3/series", headers=headers, timeout=10)
                series_response.raise_for_status()
                series_data = series_response.json()
                
                # Get queue
                queue_response = requests.get(f"{SONARR_URL}/api/v3/queue", headers=headers, timeout=10)
                queue_response.raise_for_status()
                queue_data = queue_response.json()
                
                total_episodes = sum(s.get('statistics', {}).get('episodeFileCount', 0) for s in series_data)
                total_size = sum(s.get('statistics', {}).get('sizeOnDisk', 0) for s in series_data)
                
                stats['sonarr'] = {
                    'series_count': len(series_data),
                    'episode_count': total_episodes,
                    'size_on_disk': total_size,
                    'size_gb': round(total_size / (1024**3), 2),
                    'queue_count': queue_data.get('totalRecords', 0),
                    'configured': True
                }
            except Exception as e:
                logger.error(f"Error fetching Sonarr stats: {e}")
                stats['sonarr'] = {
                    'configured': True,
                    'error': True,
                    'error_message': str(e)
                }
        else:
            stats['sonarr'] = {'configured': False}
        
        
        
        # Auto-collect stats from all integrations (plugins)
        from settings_db import get_service
        
        for integration in get_all_integrations():
            try:
                # Get config from database
                config = get_service(integration.service_name, 'default')
                
                if config:
                    service_stats = integration.get_dashboard_stats(
                        config.get('url', ''),
                        config.get('api_key', '')
                    )
                    stats[integration.service_name] = service_stats
                else:
                    stats[integration.service_name] = {'configured': False}
            except Exception as e:
                logger.error(f"Error fetching {integration.display_name} stats: {e}")
                stats[integration.service_name] = {'configured': True, 'error': True}

        
        # Episeerr stats
        from episeerr import load_config
        config = load_config()
        
        total_series_in_rules = 0
        for rule_data in config.get('rules', {}).values():
            total_series_in_rules += len(rule_data.get('series', {}))
        
        stats['episeerr'] = {
            'rule_count': len(config.get('rules', {})),
            'series_managed': total_series_in_rules
        }
        
        return jsonify({
            'success': True,
            'stats': stats
        })
        
    except Exception as e:
        logger.error(f"Error fetching dashboard stats: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e),
            'stats': {}
        }), 500


@dashboard_bp.route('/api/dashboard/activity')
def activity_feed():
    """Get most recent activity from each service"""
    try:
        services = []
        
        # Use Episeerr's own activity tracking
        activity_dir = os.path.join(os.getcwd(), 'data', 'activity')
        logger.info(f"Reading activity from: {activity_dir}")
        
        # Last search (from searches.json) - SORT BY TIMESTAMP DESC
        try:
            searches_file = os.path.join(activity_dir, 'searches.json')
            if os.path.exists(searches_file):
                with open(searches_file, 'r') as f:
                    searches = json.load(f)
                    if searches:
                        # Sort by timestamp DESC to get truly most recent
                        searches_sorted = sorted(searches, key=lambda x: x.get('timestamp', 0), reverse=True)
                        last_search = searches_sorted[0]  # Most recent after sorting
                        services.append({
                            'service': 'Sonarr',
                            'icon': 'fa-tv',
                            'color': 'primary',
                            'action': 'Searched',
                            'details': f"{last_search['series_title']} S{last_search['season']}E{last_search['episode']}",
                            'timestamp': datetime.fromtimestamp(last_search['timestamp']).isoformat(),
                            'action_icon': 'fa-search'
                        })
                        logger.info(f"Added search: {last_search['series_title']}")
            else:
                logger.warning(f"searches.json not found at {searches_file}")
        except Exception as e:
            logger.error(f"Error reading searches.json: {e}")
        
        # Last watched (from watched.json) - SORT BY TIMESTAMP DESC
        try:
            watched_file = os.path.join(activity_dir, 'watched.json')
            if os.path.exists(watched_file):
                with open(watched_file, 'r') as f:
                    watched = json.load(f)
                    if watched:
                        # Sort by timestamp DESC to get truly most recent
                        watched_sorted = sorted(watched, key=lambda x: x.get('timestamp', 0), reverse=True)
                        last_watched = watched_sorted[0]  # Most recent after sorting
                        user = last_watched.get('user', 'Unknown')
                        services.append({
                            'service': 'Jellyfin/Tautulli',
                            'icon': 'fa-eye',
                            'color': 'info',
                            'action': 'Watched',
                            'details': f"{last_watched['series_title']} S{last_watched['season']}E{last_watched['episode']} by {user}",
                            'timestamp': datetime.fromtimestamp(last_watched['timestamp']).isoformat(),
                            'action_icon': 'fa-play'
                        })
                        logger.info(f"Added watch: {last_watched['series_title']} by {user} at {last_watched['timestamp']}")
            else:
                logger.warning(f"watched.json not found at {watched_file}")
        except Exception as e:
            logger.error(f"Error reading watched.json: {e}")
        
        # Last request (from last_request.json)
        try:
            request_file = os.path.join(activity_dir, 'last_request.json')
            if os.path.exists(request_file):
                with open(request_file, 'r') as f:
                    last_req = json.load(f)
                    if last_req:
                        services.append({
                            'service': 'Jellyseerr/Overseerr',
                            'icon': 'fa-film',
                            'color': 'warning',
                            'action': 'Requested',
                            'details': f"{last_req['title']} (Season {last_req.get('requested_seasons', '?')})",
                            'timestamp': datetime.fromtimestamp(last_req['timestamp']).isoformat(),
                            'action_icon': 'fa-plus-circle'
                        })
                        logger.info(f"Added request: {last_req['title']}")
        except Exception as e:
            logger.error(f"Error reading last_request.json: {e}")
        
        # Cleanup log (from logs/cleanup.log) - get last few lines
        try:
            cleanup_log = os.path.join(os.getcwd(), 'logs', 'cleanup.log')
            if os.path.exists(cleanup_log):
                with open(cleanup_log, 'r') as f:
                    lines = f.readlines()
                    # Look for recent deletions
                    for line in reversed(lines[-50:]):  # Last 50 lines
                        if 'Deleted' in line and 'episodes' in line:
                            # Parse log line like: "2025-01-25 - Deleted 3 episodes from Show Name"
                            try:
                                parts = line.strip().split(' - ', 1)
                                if len(parts) == 2:
                                    timestamp_str, message = parts
                                    log_time = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                                    
                                    services.append({
                                        'service': 'Episeerr',
                                        'icon': 'fa-cog',
                                        'color': 'danger',
                                        'action': 'Cleanup',
                                        'details': message,
                                        'timestamp': log_time.isoformat(),
                                        'action_icon': 'fa-trash'
                                    })
                                    logger.info(f"Added cleanup: {message}")
                                    break  # Only get most recent
                            except:
                                continue
        except Exception as e:
            logger.error(f"Error reading cleanup.log: {e}")
        
        # Episeerr - Show pending deletions or recent activity
        try:
            import pending_deletions
            deletion_summary = pending_deletions.get_pending_deletions_summary()
            
            # Only show if there are pending deletions
            if deletion_summary and deletion_summary.get('total_episodes', 0) > 0:
                services.append({
                    'service': 'Episeerr',
                    'icon': 'fa-cog',
                    'color': 'purple',
                    'action': 'Pending Approval',
                    'details': f"{deletion_summary['total_episodes']} episodes ready for deletion ({deletion_summary['total_size_gb']} GB)",
                    'timestamp': datetime.now().isoformat(),
                    'action_icon': 'fa-exclamation-circle'
                })
                logger.info(f"Episeerr pending: {deletion_summary['total_episodes']} episodes")
        except Exception as e:
            logger.error(f"Error getting Episeerr activity: {e}")
        
        # Sort by timestamp (most recent first)
        services.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
        logger.info(f"Returning {len(services)} service activities")
        
        return jsonify({
            'success': True,
            'services': services
        })
        
    except Exception as e:
        logger.error(f"Error fetching activity feed: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e),
            'services': []
        }), 500
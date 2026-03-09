"""
Plex Integration for Episeerr
Provides: Now Playing widget, Watchlist with sync status, auto-request via Sonarr/Radarr,
          movie cleanup lifecycle, background sync scheduler
"""

import os
import json
import requests
import logging
import threading
import time
import xml.etree.ElementTree as ET
from typing import Dict, Any, Optional, List, Tuple
from flask import Blueprint, request, jsonify, current_app
from datetime import datetime, timedelta
from integrations.base import ServiceIntegration

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  Webhook-driven Now Playing state
#  Updated in real-time by POST /api/integration/plex/webhook
# ══════════════════════════════════════════════════════════════════

_wh_lock        = threading.Lock()
_wh_session: Optional[Dict] = None   # Current session (None = nothing playing)
_wh_last_played: Optional[Dict] = None  # Last known session (shown after stop)
_wh_updated_at: Optional[datetime] = None  # When state was last written

# Webhook state older than this is considered stale; widget falls back to polling
_WEBHOOK_STALE_SECONDS = 300  # 5 minutes

# ══════════════════════════════════════════════════════════════════
#  Episode-detection polling state  (POLLING mode only)
# ══════════════════════════════════════════════════════════════════

_plex_poll_lock    = threading.Lock()
_active_plex_sessions: Dict[str, Dict] = {}    # keyed by Plex session key
_plex_poll_threads: Dict[str, threading.Thread] = {}

# Dedup tracking for stop_threshold mode: prevents scrobble safety-net
# from double-processing an episode already handled by media.stop.
# Key: "SeriesName:SxEy"  Value: unix timestamp of processing
_processed_lock: threading.Lock = threading.Lock()
_recently_processed: Dict[str, float] = {}
_RECENT_TTL = 7200  # 2 hours — enough to span a stop → scrobble gap


def _ep_key(series_name: str, season, episode) -> str:
    return f"{series_name}:S{season}E{episode}"


def _mark_episode_processed(key: str) -> None:
    import time
    with _processed_lock:
        _recently_processed[key] = time.time()


def _was_episode_processed(key: str) -> bool:
    import time
    with _processed_lock:
        ts = _recently_processed.get(key)
        if ts and (time.time() - ts) < _RECENT_TTL:
            return True
        if ts:
            del _recently_processed[key]  # expired — clean up
        return False


def _get_plex_detection_cfg() -> Dict:
    """Load Plex episode-detection config from the database."""
    try:
        from settings_db import get_service
        svc = get_service('plex', 'default')
        if svc:
            cfg = svc.get('config') or {}
            return {
                'url':               svc.get('url', '').rstrip('/'),
                'api_key':           svc.get('api_key', ''),
                'detection_method':  cfg.get('detection_method', 'scrobble'),
                'progress_threshold': float(cfg.get('progress_threshold', 50.0)),
                'polling_interval':  int(cfg.get('polling_interval', 15)),
                'allowed_users':     cfg.get('allowed_users', []),  # [] = all users
            }
    except Exception as exc:
        logger.warning(f"[Plex] Could not load detection config: {exc}")
    return {
        'url': '', 'api_key': '',
        'detection_method': 'scrobble',
        'progress_threshold': 50.0,
        'polling_interval': 15,
        'allowed_users': [],
    }


def get_plex_watch_history(rating_key: str) -> Optional[Dict]:
    """
    Query the Plex API for the most recent view timestamp of a media item.
    Returns {'last_watched': <unix timestamp>} or None.
    """
    try:
        cfg = _get_plex_detection_cfg()
        url, api_key = cfg['url'], cfg['api_key']
        if not url or not api_key:
            return None

        resp = requests.get(
            f"{url}/library/metadata/{rating_key}",
            headers={'X-Plex-Token': api_key, 'Accept': 'application/json'},
            timeout=10,
        )
        if resp.ok:
            meta = resp.json().get('MediaContainer', {}).get('Metadata', [])
            if meta:
                last_viewed = meta[0].get('lastViewedAt')
                if last_viewed:
                    return {'last_watched': int(last_viewed)}
    except Exception as exc:
        logger.warning(f"[Plex] get_plex_watch_history({rating_key}) error: {exc}")
    return None


def get_plex_series_watch_history(series_rating_key: str) -> Optional[Dict]:
    """
    Get the most recent watch timestamp across all episodes in a Plex series.
    Returns {'last_watched': <unix timestamp>} or None.
    Endpoint: /library/metadata/{ratingKey}/children (seasons), then
              /library/metadata/{season_key}/children (episodes).
    Uses the show-level endpoint with type=4 (episode) for efficiency.
    """
    try:
        cfg = _get_plex_detection_cfg()
        url, api_key = cfg['url'], cfg['api_key']
        if not url or not api_key:
            return None

        # Fetch all episodes for the show via type=4 filter
        resp = requests.get(
            f"{url}/library/metadata/{series_rating_key}/allLeaves",
            headers={'X-Plex-Token': api_key, 'Accept': 'application/json'},
            timeout=15,
        )
        if not resp.ok:
            return None

        episodes = resp.json().get('MediaContainer', {}).get('Metadata', [])
        if not episodes:
            return None

        last_viewed = max(
            (int(ep['lastViewedAt']) for ep in episodes if ep.get('lastViewedAt')),
            default=None,
        )
        if last_viewed:
            return {'last_watched': last_viewed}
    except Exception as exc:
        logger.warning(f"[Plex] get_plex_series_watch_history({series_rating_key}) error: {exc}")
    return None


# ==========================================
# Sync Data Manager (watchlist_sync.json)
# ==========================================

SYNC_DATA_FILE = os.path.join(os.getcwd(), 'data', 'watchlist_sync.json')

def load_sync_data() -> dict:
    """Load watchlist sync tracking data"""
    try:
        if os.path.exists(SYNC_DATA_FILE):
            with open(SYNC_DATA_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading sync data: {e}")
    return {
        'synced_items': {},
        'last_full_sync': None,
        'stats': {'total_synced_tv': 0, 'total_synced_movies': 0, 'total_auto_removed': 0}
    }

def save_sync_data(data: dict):
    """Save watchlist sync tracking data"""
    try:
        os.makedirs(os.path.dirname(SYNC_DATA_FILE), exist_ok=True)
        with open(SYNC_DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error saving sync data: {e}")


class PlexIntegration(ServiceIntegration):
    """Plex integration handler"""
    
    _sync_thread = None
    _sync_running = False
    
    # ==========================================
    # Integration Metadata
    # ==========================================
    
    @property
    def service_name(self) -> str:
        return 'plex'
    
    @property
    def display_name(self) -> str:
        return 'Plex'
    
    @property
    def description(self) -> str:
        return 'Show now playing, watchlist with auto-sync, and movie cleanup'
    
    @property
    def icon(self) -> str:
        return 'https://www.plex.tv/wp-content/themes/plex/assets/img/plex-logo.svg'
    
    @property
    def category(self) -> str:
        return 'media'
    
    @property
    def default_port(self) -> int:
        return 32400
    
    # ==========================================
    # Setup Fields
    # ==========================================
    
    def get_setup_fields(self) -> Optional[List[Dict]]:
        """Custom setup fields for Plex"""
        return [
            {
                'name': 'url',
                'label': 'Plex Server URL',
                'type': 'text',
                'placeholder': 'http://192.168.1.100:32400',
                'required': True,
                'help_text': 'Your local Plex server URL (e.g., http://192.168.1.100:32400)'
            },
            {
                'name': 'api_key',
                'label': 'Plex Token',
                'type': 'password',
                'placeholder': 'Your Plex authentication token',
                'required': True,
                'help_text': 'Get from Plex Settings → Account → copy X-Plex-Token from URL'
            }
        ]
    
    def get_custom_setup_html(self, saved_values: dict = None) -> str:
        """Render episode-detection config + watchlist-sync settings below the standard fields."""
        saved_values = saved_values or {}

        # ── Episode Detection ─────────────────────────────────────
        detection_method   = saved_values.get('detection_method', 'scrobble')
        progress_threshold = saved_values.get('progress_threshold', 50)
        polling_interval   = saved_values.get('polling_interval', 15)
        allowed_users_raw  = saved_values.get('allowed_users', [])
        allowed_users_str  = ', '.join(allowed_users_raw) if isinstance(allowed_users_raw, list) else (allowed_users_raw or '')

        scrobble_sel      = 'selected' if detection_method == 'scrobble'       else ''
        polling_sel       = 'selected' if detection_method == 'polling'        else ''
        stop_thresh_sel   = 'selected' if detection_method == 'stop_threshold' else ''
        threshold_hidden  = '' if detection_method in ('polling', 'stop_threshold') else 'display:none;'
        interval_hidden   = '' if detection_method == 'polling' else 'display:none;'
        stop_help         = 'Process episode when playback stops at or beyond this percentage.' if detection_method == 'stop_threshold' else 'Process episode when playback reaches this percentage.'

        # ── Watchlist Sync ────────────────────────────────────────
        sync_config           = saved_values.get('watchlist_sync', {})
        sync_enabled          = sync_config.get('enabled', False)
        sync_interval         = sync_config.get('interval_minutes', 120)
        movie_cleanup_enabled = sync_config.get('movie_cleanup', {}).get('enabled', False)
        grace_days            = sync_config.get('movie_cleanup', {}).get('grace_days', 7)

        enabled_checked = 'checked' if sync_enabled else ''
        mc_checked      = 'checked' if movie_cleanup_enabled else ''

        sync_intervals = [
            (30, '30 minutes'), (60, '1 hour'), (120, '2 hours'),
            (240, '4 hours'), (360, '6 hours'), (720, '12 hours'), (1440, '24 hours')
        ]
        interval_options = ''.join(
            f'<option value="{v}" {"selected" if v == sync_interval else ""}>{lbl}</option>'
            for v, lbl in sync_intervals
        )

        return f'''
        <!-- ── Episode Detection ─────────────────────────────── -->
        <div style="border-top:1px solid rgba(255,255,255,0.1);margin-top:20px;padding-top:20px;">
            <h6 class="mb-3">
                <i class="fas fa-play-circle text-success me-2"></i>Episode Detection
            </h6>
            <small class="text-muted d-block mb-3">
                Requires Plex Pass for webhooks.
                Configure in Plex: <strong>Settings → Webhooks → Add Webhook</strong><br>
                URL: <code>http://&lt;episeerr-host&gt;:5002/api/integration/plex/webhook</code>
            </small>

            <div class="row">
                <div class="col-md-6 mb-3">
                    <label class="form-label">Detection Method</label>
                    <select class="form-select form-select-sm" name="plex-detection-method"
                            id="plex-detection-method"
                            onchange="(function(v){{
                                document.getElementById('plex-threshold-row').style.display=(v==='polling'||v==='stop_threshold')?'':'none';
                                document.getElementById('plex-interval-row').style.display=(v==='polling')?'':'none';
                            }})(this.value)">
                        <option value="scrobble"       {scrobble_sel}>Scrobble (Plex native — 90%)</option>
                        <option value="stop_threshold" {stop_thresh_sel}>Stop + Threshold (custom %)</option>
                        <option value="polling"        {polling_sel}>Polling (custom % — background)</option>
                    </select>
                    <small class="text-muted">
                        <strong>Scrobble:</strong> Plex&rsquo;s native 90% watched event triggers processing. Simple, no config needed.<br>
                        <strong>Stop&nbsp;+&nbsp;Threshold:</strong> Process when you stop playback at or beyond your threshold (e.g. 50%).
                        Triggers earlier than scrobble and needs no background polling.
                        Scrobble (90%) still fires automatically as a <em>safety net</em> — if the stop event was missed
                        (crash, network drop, autoplay) Episeerr catches it at 90% instead. Episodes are never double-processed.<br>
                        <strong>Polling:</strong> Background thread checks playback progress every N minutes. Useful if webhooks are unreliable.
                    </small>
                </div>
            </div>

            <div class="row">
                <div class="col-md-8 mb-3">
                    <label class="form-label">Allowed Users <small class="text-muted">(comma-separated, blank = all)</small></label>
                    <input type="text" class="form-control form-control-sm"
                           name="plex-allowed-users" value="{allowed_users_str}"
                           placeholder="e.g. Alice, Bob">
                    <small class="text-muted">Only process episodes watched by these Plex usernames. Leave blank to process all users.</small>
                </div>
            </div>

            <div id="plex-threshold-row" style="{threshold_hidden}">
                <div class="row">
                    <div class="col-md-4 mb-3">
                        <label class="form-label">Progress Threshold (%)</label>
                        <input type="number" class="form-control form-control-sm"
                               name="plex-progress-threshold" value="{progress_threshold}"
                               min="1" max="99" style="max-width:100px;">
                        <small class="text-muted">{stop_help}</small>
                    </div>
                </div>
            </div>

            <div id="plex-interval-row" style="{interval_hidden}">
                <div class="row">
                    <div class="col-md-4 mb-3">
                        <label class="form-label">Polling Interval (minutes)</label>
                        <input type="number" class="form-control form-control-sm"
                               name="plex-polling-interval" value="{polling_interval}"
                               min="1" max="60" style="max-width:100px;">
                        <small class="text-muted">How often to check playback progress</small>
                    </div>
                </div>
            </div>
        </div>

        <!-- ── Watchlist Auto-Sync ───────────────────────────── -->
        <div id="plex-watchlist-sync-settings"
             style="border-top:1px solid rgba(255,255,255,0.1);margin-top:20px;padding-top:20px;">
            <h6 class="mb-3">
                <i class="fas fa-sync-alt text-info me-2"></i>Watchlist Auto-Sync
            </h6>

            <div class="row">
                <div class="col-md-6 mb-3">
                    <div class="form-check form-switch">
                        <input type="checkbox" class="form-check-input" id="plex-sync-enabled"
                               name="plex-sync-enabled" {enabled_checked}>
                        <label class="form-check-label" for="plex-sync-enabled">Enable automatic sync</label>
                    </div>
                    <small class="text-muted">Periodically check your Plex watchlist and add new items to Sonarr/Radarr</small>
                </div>
                <div class="col-md-6 mb-3">
                    <label class="form-label">Sync Interval</label>
                    <select class="form-select form-select-sm" id="plex-sync-interval" name="plex-sync-interval">
                        {interval_options}
                    </select>
                </div>
            </div>

            <div style="border-top:1px solid rgba(255,255,255,0.05);margin-top:10px;padding-top:15px;">
                <h6 class="mb-2" style="font-size:14px;">
                    <i class="fas fa-film text-warning me-2"></i>Movie Cleanup
                </h6>
                <div class="row">
                    <div class="col-md-6 mb-3">
                        <div class="form-check form-switch">
                            <input type="checkbox" class="form-check-input" id="plex-movie-cleanup"
                                   name="plex-movie-cleanup" {mc_checked}>
                            <label class="form-check-label" for="plex-movie-cleanup">Delete movies after watched</label>
                        </div>
                        <small class="text-muted">Automatically remove watched movies from Radarr after the grace period</small>
                    </div>
                    <div class="col-md-6 mb-3">
                        <label class="form-label">Grace Period (days)</label>
                        <input type="number" class="form-control form-control-sm" id="plex-grace-days"
                               name="plex-grace-days" value="{grace_days}" min="0" max="90"
                               style="max-width:100px;">
                        <small class="text-muted">Days to keep after watching before deleting</small>
                    </div>
                </div>
            </div>
        </div>
        '''
    
    def preprocess_save_data(self, normalized_data: dict) -> None:
        """Called by save_service_config before writing to DB.
        Normalises flat form fields and nests watchlist_sync appropriately.
        """
        from settings_db import get_service
        existing_cfg  = ((get_service('plex') or {}).get('config') or {})
        existing_sync = existing_cfg.get('watchlist_sync', {})

        # ── Episode detection fields ──────────────────────────────
        normalized_data['detection_method'] = normalized_data.pop(
            'detection-method', existing_cfg.get('detection_method', 'scrobble')
        )
        normalized_data['progress_threshold'] = float(
            normalized_data.pop('progress-threshold',
                                existing_cfg.get('progress_threshold', 50.0)) or 50.0
        )
        normalized_data['polling_interval'] = int(
            normalized_data.pop('polling-interval',
                                existing_cfg.get('polling_interval', 15)) or 15
        )
        raw_users = normalized_data.pop('allowed-users', None)
        if raw_users is None:
            normalized_data['allowed_users'] = existing_cfg.get('allowed_users', [])
        else:
            normalized_data['allowed_users'] = [
                u.strip() for u in str(raw_users).split(',') if u.strip()
            ]

        # ── Watchlist sync fields ─────────────────────────────────
        sync_enabled  = normalized_data.pop('sync-enabled',  existing_sync.get('enabled', False))
        sync_interval = int(
            normalized_data.pop('sync-interval', existing_sync.get('interval_minutes', 120)) or 120
        )
        movie_cleanup = normalized_data.pop(
            'movie-cleanup', existing_sync.get('movie_cleanup', {}).get('enabled', False)
        )
        grace_days    = int(
            normalized_data.pop('grace-days', existing_sync.get('movie_cleanup', {}).get('grace_days', 7)) or 7
        )

        normalized_data['watchlist_sync'] = {
            **existing_sync,
            'enabled':          sync_enabled,
            'interval_minutes': sync_interval,
            'movie_cleanup': {
                **existing_sync.get('movie_cleanup', {}),
                'enabled':    movie_cleanup,
                'grace_days': grace_days,
            }
        }

    def on_after_save(self, normalized_data: dict) -> None:
        """Called by save_service_config after writing to DB.
        Starts or stops the watchlist sync scheduler to match the saved config.
        """
        sync_config = normalized_data.get('watchlist_sync', {})
        if sync_config.get('enabled') and not self._sync_running:
            self.start_sync_scheduler()
        elif not sync_config.get('enabled') and self._sync_running:
            self.stop_sync_scheduler()

    # ==========================================
    # Required Methods from ServiceIntegration
    # ==========================================

    def test_connection(self, url: str, api_key: str) -> Tuple[bool, str]:
        """Test Plex connection"""
        try:
            test_url = f"{url.rstrip('/')}/identity"
            headers = {'X-Plex-Token': api_key}
            
            response = requests.get(test_url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                if 'machineIdentifier' in response.text:
                    return (True, "Connected to Plex Server")
                else:
                    return (False, "Invalid response from server")
            else:
                return (False, f'Connection failed: {response.status_code}')
                
        except Exception as e:
            logger.error(f"Plex connection test failed: {e}")
            return (False, f'Connection error: {str(e)}')
    
    # ==========================================
    # Watchlist Fetching (enhanced with TMDB IDs)
    # ==========================================
    
    def fetch_watchlist(self, api_key: str) -> List[Dict]:
        """Fetch Plex watchlist with GUID parsing for TMDB/TVDB IDs"""
        items = []
        try:
            response = requests.get(
                'https://discover.provider.plex.tv/library/sections/watchlist/all?includeGuids=1',
                headers={'X-Plex-Token': api_key},
                timeout=15
            )
            
            if response.status_code != 200 or not response.text:
                logger.warning(f"Watchlist API returned {response.status_code}")
                return items
            
            root = ET.fromstring(response.text)
            videos = root.findall('.//Video')
            directories = root.findall('.//Directory')
            
            # Videos = movies, Directories = TV shows
            for element in videos + directories:
                item = {
                    'title': element.get('title'),
                    'year': element.get('year'),
                    'type': element.get('type', 'movie'),  # 'movie' or 'show'
                    'thumb': element.get('thumb'),
                    'rating_key': element.get('ratingKey'),
                    'guid': element.get('guid'),
                    'tmdb_id': None,
                    'tvdb_id': None,
                    'imdb_id': None,
                }
                
                # Parse GUIDs for external IDs
                # Plex GUIDs look like: plex://movie/5d776831880197001ec939c5
                # External IDs are in Guid sub-elements
                for guid_elem in element.findall('.//Guid'):
                    guid_id = guid_elem.get('id', '')
                    if guid_id.startswith('tmdb://'):
                        item['tmdb_id'] = guid_id.replace('tmdb://', '')
                    elif guid_id.startswith('tvdb://'):
                        item['tvdb_id'] = guid_id.replace('tvdb://', '')
                    elif guid_id.startswith('imdb://'):
                        item['imdb_id'] = guid_id.replace('imdb://', '')
                
                items.append(item)
            
            logger.info(f"Fetched {len(items)} watchlist items ({sum(1 for i in items if i['type'] == 'show')} TV, {sum(1 for i in items if i['type'] == 'movie')} movies)")
            
        except Exception as e:
            logger.error(f"Error fetching watchlist: {e}")
        
        return items
    
    # ==========================================
    # Sync Engine
    # ==========================================
    
    def get_sync_config(self) -> dict:
        """Get watchlist sync configuration with defaults"""
        defaults = {
            'enabled': False,
            'interval_minutes': 120,
            'tv_quality_profile': None,
            'tv_root_folder': None,
            'movie_quality_profile': None,
            'movie_root_folder': None,
            'movie_cleanup': {
                'enabled': False,
                'grace_days': 7,
            },
            'auto_remove_watched': True,
        }
        try:
            from settings_db import get_service
            plex_row = get_service('plex') or {}
            # watchlist_sync lives inside the config JSON column
            plex_config = plex_row.get('config') or {}
        except Exception:
            plex_config = {}

        return plex_config.get('watchlist_sync', defaults)
    
    def check_exists_in_sonarr(self, tmdb_id: str = None, tvdb_id: str = None) -> Optional[dict]:
        """Check if a show already exists in Sonarr by TMDB or TVDB ID"""
        try:
            import sonarr_utils
            prefs = sonarr_utils.load_preferences()
            headers = {'X-Api-Key': prefs['SONARR_API_KEY']}
            
            resp = requests.get(f"{prefs['SONARR_URL']}/api/v3/series", headers=headers, timeout=10)
            if not resp.ok:
                return None
            
            for series in resp.json():
                if tmdb_id and str(series.get('tmdbId')) == str(tmdb_id):
                    return series
                if tvdb_id and str(series.get('tvdbId')) == str(tvdb_id):
                    return series
            return None
        except Exception as e:
            logger.error(f"Error checking Sonarr: {e}")
            return None
    
    def check_exists_in_radarr(self, tmdb_id: str) -> Optional[dict]:
        """Check if a movie already exists in Radarr by TMDB ID"""
        try:
            from integrations import radarr as radarr_mod
            radarr_prefs = radarr_mod.load_preferences() if hasattr(radarr_mod, 'load_preferences') else None
            
            if not radarr_prefs:
                # Fallback: try settings_db
                from settings_db import get_service
                radarr_config = get_service('radarr') or {}
                radarr_url = radarr_config.get('url', '').rstrip('/')
                radarr_key = radarr_config.get('api_key', '')
            else:
                radarr_url = radarr_prefs.get('RADARR_URL', '').rstrip('/')
                radarr_key = radarr_prefs.get('RADARR_API_KEY', '')
            
            if not radarr_url or not radarr_key:
                return None
            
            headers = {'X-Api-Key': radarr_key}
            resp = requests.get(f"{radarr_url}/api/v3/movie", headers=headers, timeout=10)
            if not resp.ok:
                return None
            
            for movie in resp.json():
                if str(movie.get('tmdbId')) == str(tmdb_id):
                    return movie
            return None
        except Exception as e:
            logger.error(f"Error checking Radarr: {e}")
            return None
    
    def add_tv_to_sonarr(self, item: dict, sync_config: dict) -> dict:
        """Add a TV show to Sonarr with episeerr_select tag.
        
        Watchlist TV shows always use episeerr_select so the user gets
        a review touchpoint — pick a rule or select specific episodes.
        The sonarr_webhook detects episeerr_select → creates a pending
        selection request → sends notification → user decides.
        
        Returns: {'success': bool, 'status': str, 'series_id': int or None, 'message': str}
        """
        try:
            import sonarr_utils
            prefs = sonarr_utils.load_preferences()
            headers = {'X-Api-Key': prefs['SONARR_API_KEY'], 'Content-Type': 'application/json'}
            sonarr_url = prefs['SONARR_URL']
            
            tvdb_id = item.get('tvdb_id')
            tmdb_id = item.get('tmdb_id')
            
            if not tvdb_id and not tmdb_id:
                return {'success': False, 'status': 'missing_ids', 'series_id': None,
                        'message': f"No TVDB/TMDB ID for {item.get('title')}"}
            
            # Check if already exists FIRST
            existing = self.check_exists_in_sonarr(tmdb_id=tmdb_id, tvdb_id=tvdb_id)
            if existing:
                return {'success': True, 'status': 'already_exists', 'series_id': existing.get('id'),
                        'message': f"{item.get('title')} already in Sonarr"}
            
            # Look up series in Sonarr
            lookup_url = f"{sonarr_url}/api/v3/series/lookup"
            if tvdb_id:
                resp = requests.get(f"{lookup_url}?term=tvdb:{tvdb_id}", headers=headers, timeout=10)
            else:
                resp = requests.get(f"{lookup_url}?term=tmdb:{tmdb_id}", headers=headers, timeout=10)
            
            if not resp.ok or not resp.json():
                return {'success': False, 'status': 'lookup_failed', 'series_id': None,
                        'message': f"Could not find {item.get('title')} in Sonarr lookup"}
            
            lookup_results = resp.json()
            series_data = lookup_results[0] if isinstance(lookup_results, list) else lookup_results
            
            quality_profile = sync_config.get('tv_quality_profile')
            root_folder = sync_config.get('tv_root_folder')
            
            # Get defaults if not specified
            if not quality_profile:
                profiles_resp = requests.get(f"{sonarr_url}/api/v3/qualityprofile", headers=headers, timeout=10)
                if profiles_resp.ok and profiles_resp.json():
                    quality_profile = profiles_resp.json()[0]['id']
            
            if not root_folder:
                folders_resp = requests.get(f"{sonarr_url}/api/v3/rootfolder", headers=headers, timeout=10)
                if folders_resp.ok and folders_resp.json():
                    root_folder = folders_resp.json()[0]['path']
            
            # Get episeerr_select tag ID
            tags = []
            try:
                tag_resp = requests.get(f"{sonarr_url}/api/v3/tag", headers=headers, timeout=10)
                if tag_resp.ok:
                    existing_tags = {t['label'].lower(): t['id'] for t in tag_resp.json()}
                    if 'episeerr_select' in existing_tags:
                        tags.append(existing_tags['episeerr_select'])
                    else:
                        # Create it
                        create_resp = requests.post(f"{sonarr_url}/api/v3/tag",
                                                    headers=headers,
                                                    json={'label': 'episeerr_select'},
                                                    timeout=10)
                        if create_resp.ok:
                            tags.append(create_resp.json()['id'])
            except Exception as tag_err:
                logger.warning(f"Error setting episeerr_select tag: {tag_err}")
            
            add_payload = {
                'tvdbId': series_data.get('tvdbId'),
                'title': series_data.get('title'),
                'qualityProfileId': int(quality_profile) if quality_profile else 1,
                'rootFolderPath': root_folder or '/tv',
                'monitored': True,
                'seasonFolder': True,
                'tags': tags,
                'addOptions': {
                    'monitor': 'none',  # Don't monitor anything yet — user decides
                    'searchForMissingEpisodes': False
                }
            }
            
            add_resp = requests.post(f"{sonarr_url}/api/v3/series", headers=headers,
                                     json=add_payload, timeout=15)
            
            if add_resp.ok:
                series_id = add_resp.json().get('id')
                logger.info(f"✅ Added TV show to Sonarr: {item.get('title')} (ID: {series_id}) "
                           f"with episeerr_select tag — awaiting rule/episode selection")
                return {'success': True, 'status': 'added', 'series_id': series_id,
                        'message': f"Added {item.get('title')} — pending selection"}
            elif add_resp.status_code == 400 and 'already been added' in add_resp.text.lower():
                return {'success': True, 'status': 'already_exists', 'series_id': None,
                        'message': f"{item.get('title')} already in Sonarr"}
            else:
                return {'success': False, 'status': 'add_failed', 'series_id': None,
                        'message': f"Sonarr add failed ({add_resp.status_code}): {add_resp.text[:200]}"}
        
        except Exception as e:
            logger.error(f"Error adding TV to Sonarr: {e}")
            return {'success': False, 'status': 'error', 'series_id': None, 'message': str(e)}
    
    def add_movie_to_radarr(self, item: dict, sync_config: dict) -> dict:
        """Add a movie to Radarr directly
        
        Returns: {'success': bool, 'status': str, 'movie_id': int or None, 'message': str}
        """
        try:
            from settings_db import get_service
            radarr_config = get_service('radarr') or {}
            radarr_url = radarr_config.get('url', '').rstrip('/')
            radarr_key = radarr_config.get('api_key', '')
            
            if not radarr_url or not radarr_key:
                return {'success': False, 'status': 'not_configured', 'movie_id': None,
                        'message': 'Radarr not configured'}
            
            headers = {'X-Api-Key': radarr_key, 'Content-Type': 'application/json'}
            tmdb_id = item.get('tmdb_id')
            
            if not tmdb_id:
                return {'success': False, 'status': 'missing_id', 'movie_id': None,
                        'message': f"No TMDB ID for {item.get('title')}"}
            
            # Lookup movie in Radarr
            lookup_resp = requests.get(f"{radarr_url}/api/v3/movie/lookup/tmdb?tmdbId={tmdb_id}",
                                       headers=headers, timeout=10)
            
            if not lookup_resp.ok:
                return {'success': False, 'status': 'lookup_failed', 'movie_id': None,
                        'message': f"Radarr lookup failed for {item.get('title')}"}
            
            movie_data = lookup_resp.json()
            
            quality_profile = sync_config.get('movie_quality_profile')
            root_folder = sync_config.get('movie_root_folder')
            
            # Get defaults if not specified
            if not quality_profile:
                profiles_resp = requests.get(f"{radarr_url}/api/v3/qualityprofile", headers=headers, timeout=10)
                if profiles_resp.ok and profiles_resp.json():
                    quality_profile = profiles_resp.json()[0]['id']
            
            if not root_folder:
                folders_resp = requests.get(f"{radarr_url}/api/v3/rootfolder", headers=headers, timeout=10)
                if folders_resp.ok and folders_resp.json():
                    root_folder = folders_resp.json()[0]['path']
            
            add_payload = {
                'tmdbId': int(tmdb_id),
                'title': movie_data.get('title', item.get('title')),
                'qualityProfileId': int(quality_profile) if quality_profile else 1,
                'rootFolderPath': root_folder or '/movies',
                'monitored': True,
                'addOptions': {
                    'searchForMovie': True  # Immediately search for the movie
                }
            }
            
            add_resp = requests.post(f"{radarr_url}/api/v3/movie", headers=headers,
                                     json=add_payload, timeout=15)
            
            if add_resp.ok:
                movie_id = add_resp.json().get('id')
                logger.info(f"✅ Added movie to Radarr: {item.get('title')} (ID: {movie_id})")
                return {'success': True, 'status': 'added', 'movie_id': movie_id,
                        'message': f"Added {item.get('title')} to Radarr"}
            elif add_resp.status_code == 400 and 'already been added' in add_resp.text.lower():
                return {'success': True, 'status': 'already_exists', 'movie_id': None,
                        'message': f"{item.get('title')} already in Radarr"}
            else:
                return {'success': False, 'status': 'add_failed', 'movie_id': None,
                        'message': f"Radarr add failed ({add_resp.status_code}): {add_resp.text[:200]}"}
        
        except Exception as e:
            logger.error(f"Error adding movie to Radarr: {e}")
            return {'success': False, 'status': 'error', 'movie_id': None, 'message': str(e)}
    
    def sync_watchlist(self) -> dict:
        """Main sync method - fetch watchlist, process new items, return results
        
        Returns summary dict with counts and per-item results.
        """
        try:
            from settings_db import get_service
            plex_config = get_service('plex') or {}
            api_key = plex_config.get('api_key')
            
            if not api_key:
                return {'success': False, 'message': 'Plex not configured'}
            
            sync_config = self.get_sync_config()
            sync_data = load_sync_data()
            
            # Fetch current watchlist
            watchlist_items = self.fetch_watchlist(api_key)
            
            if not watchlist_items:
                return {'success': True, 'message': 'Watchlist empty', 'processed': 0}
            
            results = {
                'success': True,
                'processed': 0,
                'skipped': 0,
                'added_tv': 0,
                'added_movies': 0,
                'already_exists': 0,
                'errors': 0,
                'items': []
            }
            
            for item in watchlist_items:
                item_key = f"{item['type']}_{item.get('tmdb_id') or item.get('rating_key')}"
                
                # Skip if already synced and in a terminal state
                if item_key in sync_data['synced_items']:
                    existing = sync_data['synced_items'][item_key]
                    if existing.get('status') in ('added_to_sonarr', 'added_to_radarr', 
                                                   'already_exists', 'watched', 'cleaned_up',
                                                   'pending_selection'):
                        results['skipped'] += 1
                        continue
                    # If previous attempt errored, retry
                
                # ── TV Shows ──────────────────────────────────────────
                if item.get('type') == 'show':
                    # Always check Sonarr first — if it's there, don't touch it
                    existing_series = self.check_exists_in_sonarr(
                        tmdb_id=item.get('tmdb_id'), tvdb_id=item.get('tvdb_id'))
                    
                    if existing_series:
                        # Also check if it's already in an Episeerr rule
                        sonarr_id = existing_series.get('id')
                        in_episeerr = False
                        try:
                            from episeerr import load_config as load_episeerr_config
                            ep_config = load_episeerr_config()
                            for rule_name, rule_data in ep_config.get('rules', {}).items():
                                if str(sonarr_id) in rule_data.get('series', {}):
                                    in_episeerr = True
                                    break
                        except Exception:
                            pass
                        
                        sync_data['synced_items'][item_key] = {
                            'tmdb_id': item.get('tmdb_id'),
                            'tvdb_id': item.get('tvdb_id'),
                            'title': item['title'],
                            'type': 'tv',
                            'synced_at': datetime.now().isoformat(),
                            'source': 'watchlist_sync',
                            'status': 'already_exists',
                            'sonarr_series_id': sonarr_id,
                            'in_episeerr': in_episeerr
                        }
                        results['already_exists'] += 1
                        results['processed'] += 1
                        results['items'].append({
                            'title': item['title'], 'type': 'show',
                            'status': 'already_exists', 'message': f"Already in Sonarr{' + Episeerr' if in_episeerr else ''}"
                        })
                        continue
                    
                    # Check if there's already a pending selection request for this show
                    has_pending = False
                    try:
                        import os as _os
                        requests_dir = _os.path.join(_os.getcwd(), 'data', 'requests')
                        if _os.path.exists(requests_dir):
                            for fname in _os.listdir(requests_dir):
                                if fname.endswith('.json'):
                                    try:
                                        with open(_os.path.join(requests_dir, fname), 'r') as f:
                                            req = json.load(f)
                                            if str(req.get('tmdb_id')) == str(item.get('tmdb_id')):
                                                has_pending = True
                                                break
                                    except Exception:
                                        continue
                    except Exception:
                        pass
                    
                    if has_pending:
                        sync_data['synced_items'][item_key] = {
                            'tmdb_id': item.get('tmdb_id'),
                            'tvdb_id': item.get('tvdb_id'),
                            'title': item['title'],
                            'type': 'tv',
                            'synced_at': datetime.now().isoformat(),
                            'source': 'watchlist_sync',
                            'status': 'pending_selection',
                        }
                        results['skipped'] += 1
                        results['processed'] += 1
                        results['items'].append({
                            'title': item['title'], 'type': 'show',
                            'status': 'pending_selection', 'message': 'Already has pending selection request'
                        })
                        continue
                    
                    # Not in Sonarr — add it
                    result = self.add_tv_to_sonarr(item, sync_config)
                    sync_data['synced_items'][item_key] = {
                        'tmdb_id': item.get('tmdb_id'),
                        'tvdb_id': item.get('tvdb_id'),
                        'title': item['title'],
                        'type': 'tv',
                        'synced_at': datetime.now().isoformat(),
                        'source': 'watchlist_sync',
                        'status': 'added_to_sonarr' if result['success'] else result.get('status', 'error'),
                        'sonarr_series_id': result.get('series_id'),
                    }
                    if result['success'] and result.get('status') != 'already_exists':
                        results['added_tv'] += 1
                        sync_data['stats']['total_synced_tv'] += 1
                    elif result.get('status') == 'already_exists':
                        results['already_exists'] += 1
                    else:
                        results['errors'] += 1
                
                # ── Movies ────────────────────────────────────────────
                elif item.get('type') == 'movie':
                    # Always check Radarr first
                    if item.get('tmdb_id'):
                        existing_movie = self.check_exists_in_radarr(item['tmdb_id'])
                    else:
                        existing_movie = None
                    
                    if existing_movie:
                        sync_data['synced_items'][item_key] = {
                            'tmdb_id': item.get('tmdb_id'),
                            'title': item['title'],
                            'type': 'movie',
                            'synced_at': datetime.now().isoformat(),
                            'source': 'watchlist_sync',
                            'status': 'already_exists',
                            'radarr_movie_id': existing_movie.get('id'),
                            'watched': False, 'watched_at': None, 'cleanup_eligible_at': None
                        }
                        results['already_exists'] += 1
                        results['processed'] += 1
                        results['items'].append({
                            'title': item['title'], 'type': 'movie',
                            'status': 'already_exists', 'message': 'Already in Radarr'
                        })
                        continue
                    
                    # Not in Radarr — add it
                    result = self.add_movie_to_radarr(item, sync_config)
                    sync_data['synced_items'][item_key] = {
                        'tmdb_id': item.get('tmdb_id'),
                        'title': item['title'],
                        'type': 'movie',
                        'synced_at': datetime.now().isoformat(),
                        'source': 'watchlist_sync',
                        'status': 'added_to_radarr' if result['success'] else result.get('status', 'error'),
                        'radarr_movie_id': result.get('movie_id'),
                        'watched': False, 'watched_at': None, 'cleanup_eligible_at': None
                    }
                    if result['success'] and result.get('status') != 'already_exists':
                        results['added_movies'] += 1
                        sync_data['stats']['total_synced_movies'] += 1
                    elif result.get('status') == 'already_exists':
                        results['already_exists'] += 1
                    else:
                        results['errors'] += 1
                
                else:
                    # Unknown type, skip
                    results['skipped'] += 1
                    continue
                
                results['processed'] += 1
                results['items'].append({
                    'title': item['title'],
                    'type': item['type'],
                    'status': result.get('status', 'unknown'),
                    'message': result.get('message', '')
                })
            
            sync_data['last_full_sync'] = datetime.now().isoformat()
            save_sync_data(sync_data)
            
            logger.info(f"Watchlist sync complete: {results['added_tv']} TV added, "
                        f"{results['added_movies']} movies added, {results['already_exists']} existed, "
                        f"{results['errors']} errors")
            
            return results
            
        except Exception as e:
            logger.error(f"Watchlist sync error: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}
    
    # ==========================================
    # Movie Cleanup Lifecycle
    # ==========================================
    
    def mark_item_watched(self, tmdb_id: str, media_type: str):
        """Called from Tautulli webhook handler when something is watched.
        
        For movies: marks as watched and sets cleanup_eligible_at.
        For TV: checks if series is fully watched for watchlist removal.
        """
        sync_data = load_sync_data()
        sync_config = self.get_sync_config()
        grace_days = sync_config.get('movie_cleanup', {}).get('grace_days', 7)
        
        # Find the item in sync data
        for item_key, item in sync_data['synced_items'].items():
            if str(item.get('tmdb_id')) == str(tmdb_id):
                if media_type == 'movie' and item['type'] == 'movie':
                    item['watched'] = True
                    item['watched_at'] = datetime.now().isoformat()
                    item['cleanup_eligible_at'] = (datetime.now() + timedelta(days=grace_days)).isoformat()
                    item['status'] = 'watched'
                    logger.info(f"🎬 Movie watched: {item['title']} - cleanup eligible in {grace_days} days")
                elif media_type == 'tv' and item['type'] == 'tv':
                    # For TV, just update the watched timestamp
                    # Full series completion check would need Sonarr data
                    item['last_watched'] = datetime.now().isoformat()
                    logger.info(f"📺 TV watched update: {item['title']}")
                break
        
        save_sync_data(sync_data)
    
    def cleanup_watched_movies(self) -> dict:
        """Check for watched movies past their grace period and clean them up.
        
        Called from the existing cleanup cycle (conditionally).
        Returns: {'cleaned': int, 'pending': int, 'items': [...]}
        """
        sync_data = load_sync_data()
        sync_config = self.get_sync_config()
        movie_cleanup = sync_config.get('movie_cleanup', {})
        
        if not movie_cleanup.get('enabled'):
            return {'cleaned': 0, 'pending': 0, 'items': []}
        
        now = datetime.now()
        results = {'cleaned': 0, 'pending': 0, 'items': []}
        
        for item_key, item in list(sync_data['synced_items'].items()):
            if item['type'] != 'movie' or not item.get('watched'):
                continue
            
            if not item.get('cleanup_eligible_at'):
                continue
            
            eligible_at = datetime.fromisoformat(item['cleanup_eligible_at'])
            
            if now >= eligible_at:
                # Time to clean up
                try:
                    radarr_movie_id = item.get('radarr_movie_id')
                    if radarr_movie_id:
                        from settings_db import get_service
                        radarr_config = get_service('radarr') or {}
                        radarr_url = radarr_config.get('url', '').rstrip('/')
                        radarr_key = radarr_config.get('api_key', '')
                        
                        if radarr_url and radarr_key:
                            headers = {'X-Api-Key': radarr_key}
                            del_resp = requests.delete(
                                f"{radarr_url}/api/v3/movie/{radarr_movie_id}?deleteFiles=true",
                                headers=headers, timeout=15)
                            
                            if del_resp.ok:
                                logger.info(f"🗑️ Cleaned up movie: {item['title']}")
                                item['status'] = 'cleaned_up'
                                item['cleaned_at'] = now.isoformat()
                                results['cleaned'] += 1
                                sync_data['stats']['total_auto_removed'] += 1
                                
                                results['items'].append({
                                    'title': item['title'],
                                    'action': 'deleted'
                                })
                            else:
                                logger.warning(f"Failed to delete movie {item['title']}: {del_resp.status_code}")
                except Exception as e:
                    logger.error(f"Error cleaning up movie {item.get('title')}: {e}")
            else:
                days_left = (eligible_at - now).days
                results['pending'] += 1
                results['items'].append({
                    'title': item['title'],
                    'action': 'pending',
                    'days_left': days_left
                })
        
        save_sync_data(sync_data)
        return results
    
    # ==========================================
    # Background Scheduler
    # ==========================================
    
    def start_sync_scheduler(self):
        """Start background watchlist sync thread.
        
        Call this from initialize_episeerr() with:
            plex_config = get_service('plex') or {}
            sync_cfg = plex_config.get('watchlist_sync', {})
            if sync_cfg.get('enabled') and plex_config.get('api_key'):
                from integrations.plex import integration as plex_integration
                plex_integration.start_sync_scheduler()
        """
        if self._sync_running:
            logger.info("Watchlist sync scheduler already running")
            return
        
        sync_config = self.get_sync_config()
        if not sync_config.get('enabled'):
            logger.info("Watchlist sync disabled - scheduler not started")
            return
        
        interval = sync_config.get('interval_minutes', 120)
        
        def sync_loop():
            self._sync_running = True
            # Initial delay to let the app fully start
            time.sleep(30)
            
            while self._sync_running:
                try:
                    logger.info("⏰ Running scheduled watchlist sync...")
                    self.sync_watchlist()
                    
                    # Also run movie cleanup if enabled
                    cleanup_result = self.cleanup_watched_movies()
                    if cleanup_result['cleaned'] > 0:
                        logger.info(f"Movie cleanup: {cleanup_result['cleaned']} removed")
                    
                except Exception as e:
                    logger.error(f"Scheduled sync error: {e}", exc_info=True)
                
                # Re-read interval in case it changed
                try:
                    current_config = self.get_sync_config()
                    interval = current_config.get('interval_minutes', 120)
                except:
                    pass
                
                time.sleep(interval * 60)
        
        self._sync_thread = threading.Thread(target=sync_loop, daemon=True, name='plex-watchlist-sync')
        self._sync_thread.start()
        logger.info(f"✅ Plex watchlist sync scheduler started (every {interval} minutes)")
    
    def stop_sync_scheduler(self):
        """Stop the background sync thread"""
        self._sync_running = False
        logger.info("Plex watchlist sync scheduler stopped")
    
    # ==========================================
    # Poster Status Helpers
    # ==========================================
    
    def get_watchlist_with_status(self, api_key: str) -> List[Dict]:
        """Fetch watchlist and enrich each item with its sync/download status.
        
        # Status values:
        # - 'on_watchlist'    : Not yet synced (sync disabled or new item)
        # - 'pending'         : Added to Sonarr, awaiting rule/episode selection
        # - 'requested'       : Synced, waiting for download
        # - 'downloading'     : Currently being downloaded
        # - 'available'       : Downloaded / available to watch
        # - 'watched'         : Watched, pending cleanup
        # - 'error'           : Sync attempted but failed
        """
        items = self.fetch_watchlist(api_key)
        sync_data = load_sync_data()
        
        # Build lookup of what's in Sonarr/Radarr
        sonarr_by_tmdb = {}
        radarr_by_tmdb = {}
        
        try:
            import sonarr_utils
            prefs = sonarr_utils.load_preferences()
            headers = {'X-Api-Key': prefs['SONARR_API_KEY']}
            resp = requests.get(f"{prefs['SONARR_URL']}/api/v3/series", headers=headers, timeout=10)
            if resp.ok:
                for s in resp.json():
                    if s.get('tmdbId'):
                        sonarr_by_tmdb[str(s['tmdbId'])] = s
            
            # Also get tag mapping for detecting episeerr_select
            sonarr_tag_map = {}
            tag_resp = requests.get(f"{prefs['SONARR_URL']}/api/v3/tag", headers=headers, timeout=10)
            if tag_resp.ok:
                sonarr_tag_map = {t['id']: t['label'].lower() for t in tag_resp.json()}
        except Exception as e:
            logger.debug(f"Could not load Sonarr series for status: {e}")
            sonarr_tag_map = {}
        
        try:
            from settings_db import get_service
            radarr_config = get_service('radarr') or {}
            radarr_url = radarr_config.get('url', '').rstrip('/')
            radarr_key = radarr_config.get('api_key', '')
            if radarr_url and radarr_key:
                headers = {'X-Api-Key': radarr_key}
                resp = requests.get(f"{radarr_url}/api/v3/movie", headers=headers, timeout=10)
                if resp.ok:
                    for m in resp.json():
                        if m.get('tmdbId'):
                            radarr_by_tmdb[str(m['tmdbId'])] = m
        except Exception as e:
            logger.debug(f"Could not load Radarr movies for status: {e}")
        
        # Enrich each watchlist item
        for item in items:
            tmdb_id = item.get('tmdb_id')
            item_key = f"{item['type']}_{tmdb_id or item.get('rating_key')}"
            
            # Default state
            item['sync_status'] = 'on_watchlist'
            item['status_label'] = 'On Watchlist'
            item['status_color'] = '#6c757d'  # gray
            
            # Check sync data for this item
            synced = sync_data.get('synced_items', {}).get(item_key)
            
            if synced:
                status = synced.get('status', '')
                
                if status in ('error', 'add_failed', 'lookup_failed', 'missing_ids', 'missing_id'):
                    item['sync_status'] = 'error'
                    item['status_label'] = 'Error'
                    item['status_color'] = '#dc3545'  # red
                    continue
                
                if synced.get('watched'):
                    item['sync_status'] = 'watched'
                    item['status_label'] = 'Watched'
                    item['status_color'] = '#198754'  # green
                    continue
            
            # Check if available in *arrs
            if item.get('type') == 'show' and tmdb_id and tmdb_id in sonarr_by_tmdb:
                series = sonarr_by_tmdb[tmdb_id]
                ep_count = series.get('episodeFileCount', 0)
                
                # Check if it has episeerr_select tag (pending selection)
                has_select_tag = False
                series_tags = series.get('tags', [])
                if series_tags:
                    for tag_id in series_tags:
                        tag_label = sonarr_tag_map.get(tag_id, '')
                        if tag_label == 'episeerr_select':
                            has_select_tag = True
                            break
                
                if has_select_tag and ep_count == 0:
                    item['sync_status'] = 'pending'
                    item['status_label'] = 'Needs Setup'
                    item['status_color'] = '#fd7e14'  # orange
                elif ep_count > 0:
                    item['sync_status'] = 'available'
                    item['status_label'] = f'{ep_count} eps'
                    item['status_color'] = '#0d6efd'  # blue
                else:
                    item['sync_status'] = 'requested'
                    item['status_label'] = 'Requested'
                    item['status_color'] = '#ffc107'  # yellow
            
            elif item.get('type') == 'movie' and tmdb_id and tmdb_id in radarr_by_tmdb:
                movie = radarr_by_tmdb[tmdb_id]
                if movie.get('hasFile'):
                    item['sync_status'] = 'available'
                    item['status_label'] = 'Ready'
                    item['status_color'] = '#0d6efd'  # blue
                else:
                    item['sync_status'] = 'requested'
                    item['status_label'] = 'Requested'
                    item['status_color'] = '#ffc107'  # yellow
        
        return items
    
    # ==========================================
    # Dashboard Stats (enhanced)
    # ==========================================
    
    def get_dashboard_stats(self, url: str, api_key: str) -> Dict[str, Any]:
        """Get Plex stats for dashboard"""
        try:
            server_url = url.rstrip('/')
            headers = {'X-Plex-Token': api_key}
            
            # Get watchlist with status enrichment
            watchlist_items = self.get_watchlist_with_status(api_key)
            watchlist_count = len(watchlist_items)
            
            # Get now playing / sessions
            now_playing = None
            try:
                sessions_response = requests.get(
                    f"{server_url}/status/sessions",
                    headers=headers,
                    timeout=10
                )
                
                if sessions_response.status_code == 200 and sessions_response.text:
                    try:
                        root = ET.fromstring(sessions_response.text)
                        video = root.find('.//Video')
                        
                        if video is not None:
                            player = video.find('Player')
                            user = video.find('User')
                            
                            view_offset = int(video.get('viewOffset', 0))
                            duration = int(video.get('duration', 1))
                            progress = int((view_offset / duration) * 100) if duration > 0 else 0
                            
                            thumb = video.get('thumb')
                            art = video.get('art')
                            
                            now_playing = {
                                'title': video.get('grandparentTitle') or video.get('title'),
                                'episode_title': video.get('title') if video.get('grandparentTitle') else None,
                                'season': video.get('parentIndex'),
                                'episode': video.get('index'),
                                'thumb': f"{server_url}{thumb}?X-Plex-Token={api_key}" if thumb else None,
                                'art': f"{server_url}{art}?X-Plex-Token={api_key}" if art else None,
                                'user': user.get('title') if user is not None else 'Unknown',
                                'state': player.get('state') if player is not None else 'stopped',
                                'progress': progress,
                                'type': video.get('type')
                            }
                            logger.info(f"Plex now playing: {now_playing.get('title')}")
                    except ET.ParseError as pe:
                        logger.error(f"XML parse error: {pe}")
                    except Exception as xe:
                        logger.error(f"Error parsing session XML: {xe}")
            except Exception as e:
                logger.error(f"Error fetching sessions: {e}")
            
            # Get sync status for dashboard
            sync_data = load_sync_data()
            
            return {
                'configured': True,
                'watchlist_count': watchlist_count,
                'watchlist_items': watchlist_items,
                'now_playing': now_playing,
                'sync': {
                    'last_sync': sync_data.get('last_full_sync'),
                    'total_synced_tv': sync_data.get('stats', {}).get('total_synced_tv', 0),
                    'total_synced_movies': sync_data.get('stats', {}).get('total_synced_movies', 0),
                    'total_auto_removed': sync_data.get('stats', {}).get('total_auto_removed', 0),
                }
            }
            
        except Exception as e:
            logger.error(f"Plex stats error: {e}")
            return {'configured': True, 'error': str(e)}
    
    def get_dashboard_widget(self) -> Dict[str, Any]:
        """Define dashboard pill"""
        return {
            'enabled': True,
            'pill': {
                'icon': 'fas fa-eye',
                'icon_color': 'text-warning',
                'template': '{watchlist_count}',
                'fields': ['watchlist_count']
            },
            'has_custom_widget': True,
            'has_dashboard_section': True
        }
    
    # ══════════════════════════════════════════════════════════════
    # Episode Detection (Scrobble / Polling)
    # ══════════════════════════════════════════════════════════════

    def process_episode(self, episode_info: Dict) -> bool:
        """
        Process a Plex episode for Sonarr upgrade logic.
        Writes a temp file and spawns media_processor.py — same pattern
        as the Jellyfin/Emby integrations.
        """
        import subprocess
        from media_processor import get_series_id, get_episode_tracking_key
        from episeerr import load_config, save_config
        import episeerr_utils

        series_name = episode_info.get('series_name', '')
        season      = episode_info.get('season_number')
        episode     = episode_info.get('episode_number')
        user        = episode_info.get('user_name', 'Unknown')
        progress    = episode_info.get('progress_percent', 0)

        try:
            series_id = get_series_id(series_name)
            final_rule = None

            if series_id:
                from episeerr_utils import validate_series_tag, sync_rule_tag_to_sonarr
                from media_processor import move_series_in_config
                config = load_config()
                series_id_str = str(series_id)
                config_rule = None

                for rule_name, rule_details in config['rules'].items():
                    if series_id_str in rule_details.get('series', {}):
                        config_rule = rule_name
                        break

                if config_rule:
                    matches, actual_tag_rule = validate_series_tag(series_id, config_rule)
                    if not matches:
                        if actual_tag_rule:
                            logger.warning(f"[Plex] DRIFT: config={config_rule} → tag={actual_tag_rule}")
                            move_series_in_config(series_id, config_rule, actual_tag_rule)
                            final_rule = actual_tag_rule
                        else:
                            logger.warning(f"[Plex] No tag on {series_id} → restoring episeerr_{config_rule}")
                            sync_rule_tag_to_sonarr(series_id, config_rule)
                            final_rule = config_rule
                    else:
                        final_rule = config_rule

            temp_dir  = os.path.join(os.getcwd(), 'temp')
            os.makedirs(temp_dir, exist_ok=True)

            payload = {
                'server_title':     series_name,
                'server_season_num': int(season),
                'server_ep_num':    int(episode),
                'sonarr_series_id': series_id,
                'rule':             final_rule,
                'source':           'plex',
            }

            temp_path = os.path.join(temp_dir, 'data_from_server.json')
            with open(temp_path, 'w') as fh:
                import json as _json
                _json.dump(payload, fh)

            result = subprocess.run(
                ["python3", os.path.join(os.getcwd(), "media_processor.py")],
                capture_output=True, text=True,
            )

            if result.returncode != 0:
                logger.error(f"[Plex] media_processor failed (rc={result.returncode}): {result.stderr}")
                return False

            logger.info(f"[Plex] Processed {series_name} S{season}E{episode} for {user} at {progress:.1f}%")
            return True

        except Exception as exc:
            logger.error(f"[Plex] process_episode error: {exc}", exc_info=True)
            return False

    def poll_plex_session(self, session_key: str, episode_info: Dict):
        """Background thread: poll /status/sessions until threshold or session ends."""
        cfg              = _get_plex_detection_cfg()
        url              = cfg['url']
        api_key          = cfg['api_key']
        threshold        = cfg['progress_threshold']
        interval_seconds = cfg['polling_interval'] * 60

        logger.info(
            f"[Plex] Polling started for session {session_key}: "
            f"{episode_info.get('series_name')} S{episode_info.get('season_number')}E{episode_info.get('episode_number')} "
            f"(trigger at {threshold}%)"
        )

        try:
            processed  = False
            poll_count = 0

            while session_key in _active_plex_sessions and not processed:
                poll_count += 1
                current_progress = 0.0

                try:
                    resp = requests.get(
                        f"{url}/status/sessions",
                        headers={'X-Plex-Token': api_key},
                        timeout=10,
                    )
                    if resp.ok and resp.text:
                        import xml.etree.ElementTree as ET
                        root = ET.fromstring(resp.text)
                        for video in root.findall('.//Video'):
                            if video.get('sessionKey') == session_key:
                                view_offset = int(video.get('viewOffset', 0))
                                duration    = int(video.get('duration', 1))
                                current_progress = (view_offset / duration * 100) if duration else 0
                                break
                        else:
                            # Session no longer in active list
                            logger.info(f"[Plex] Session {session_key} ended — stopping polling (poll #{poll_count})")
                            break
                    else:
                        logger.debug(f"[Plex] Sessions API returned {resp.status_code}")

                except Exception as poll_err:
                    logger.warning(f"[Plex] Poll error for {session_key}: {poll_err}")

                logger.info(f"[Plex] Poll #{poll_count}: {current_progress:.1f}% (threshold {threshold}%)")

                if current_progress >= threshold:
                    logger.info(f"[Plex] Threshold reached — processing episode")
                    ep_info = {**episode_info, 'progress_percent': current_progress}
                    success = self.process_episode(ep_info)
                    if success:
                        processed = True

                if not processed:
                    time.sleep(interval_seconds)

        except Exception as exc:
            logger.error(f"[Plex] poll_plex_session error for {session_key}: {exc}", exc_info=True)
        finally:
            with _plex_poll_lock:
                _active_plex_sessions.pop(session_key, None)
                _plex_poll_threads.pop(session_key, None)
            logger.info(f"[Plex] Polling cleaned up for session {session_key}")

    def start_polling(self, session_key: str, episode_info: Dict) -> bool:
        with _plex_poll_lock:
            if session_key in _active_plex_sessions:
                logger.info(f"[Plex] Already polling session {session_key} — skipping")
                return False
            _active_plex_sessions[session_key] = episode_info

        thread = threading.Thread(
            target=self.poll_plex_session,
            args=(session_key, episode_info),
            daemon=True,
            name=f"PlexPoll-{session_key[:8]}",
        )
        thread.start()
        with _plex_poll_lock:
            _plex_poll_threads[session_key] = thread
        return True

    def stop_polling(self, session_key: str) -> bool:
        with _plex_poll_lock:
            if session_key in _active_plex_sessions:
                logger.info(f"[Plex] Stopping polling for session {session_key}")
                del _active_plex_sessions[session_key]
                return True
        return False

    # ==========================================
    # Flask Routes
    # ==========================================

    def create_blueprint(self) -> Blueprint:
        """Create Flask blueprint with Plex-specific routes"""
        bp = Blueprint('plex_integration', __name__, url_prefix='/api/integration/plex')
        integration = self
        
        @bp.route('/debug-sessions')
        def debug_sessions():
            """Debug endpoint to see raw sessions XML"""
            try:
                from settings_db import get_service
                
                plex_config = get_service('plex')
                if not plex_config:
                    return "Plex not configured in settings_db", 404
                
                url = plex_config.get('url')
                api_key = plex_config.get('api_key')
                
                if not url or not api_key:
                    return f"Missing config - URL: {url}, API Key: {'present' if api_key else 'missing'}", 400
                
                full_url = f"{url.rstrip('/')}/status/sessions?X-Plex-Token={api_key}"
                response = requests.get(full_url, timeout=10)
                
                return f"<h3>Status: {response.status_code}</h3><pre>{response.text}</pre>", 200, {'Content-Type': 'text/html'}
                
            except Exception as e:
                import traceback
                return f"<pre>Error: {str(e)}\n\n{traceback.format_exc()}</pre>", 500, {'Content-Type': 'text/html'}
        
        # ── Plex webhook receiver ─────────────────────────────────────
        @bp.route('/webhook', methods=['POST'])
        def webhook():
            """
            Receive Plex webhook events.

            Drives two things simultaneously:
              1. Dashboard "Now Playing" widget state (all play/pause/stop events)
              2. Episode detection for Sonarr management:
                   scrobble mode → process on media.scrobble (90% Plex native)
                   polling mode  → start polling on media.play; process at threshold %

            Requires Plex Pass.  Configure in Plex:
              Settings → Webhooks → Add Webhook
              URL: http://<episeerr-host>:5002/api/integration/plex/webhook

            Plex sends multipart/form-data with a 'payload' field containing JSON.
            """
            global _wh_session, _wh_last_played, _wh_updated_at

            try:
                raw = request.form.get('payload') or request.get_data(as_text=True)
                if not raw:
                    return jsonify({'status': 'error', 'message': 'Empty payload'}), 400
                data = json.loads(raw)
            except Exception as exc:
                logger.warning(f"[Plex webhook] Could not parse payload: {exc}")
                return jsonify({'status': 'error', 'message': 'Invalid JSON'}), 400

            event       = data.get('event', '')
            metadata    = data.get('Metadata', {})
            player      = data.get('Player', {})
            account     = data.get('Account', {})
            media_type  = metadata.get('type', '')          # 'episode', 'movie', etc.
            session_key = player.get('key') or player.get('machineIdentifier', '')

            logger.debug(f"[Plex webhook] event={event!r} type={media_type!r} title={metadata.get('title')!r}")

            now = datetime.now()

            # ── 1. Dashboard display state ────────────────────────────
            if event in ('media.play', 'media.resume', 'media.pause'):
                view_offset = int(metadata.get('viewOffset', 0))
                duration    = int(metadata.get('duration', 0))
                progress    = int((view_offset / duration) * 100) if duration else 0
                state       = 'paused' if event == 'media.pause' else player.get('state', 'playing')
                raw_thumb   = metadata.get('thumb') or metadata.get('art')

                wh_session_data = {
                    'title':         metadata.get('grandparentTitle') or metadata.get('title', 'Unknown'),
                    'episode_title': metadata.get('title') if metadata.get('grandparentTitle') else None,
                    'season':        metadata.get('parentIndex'),
                    'episode':       metadata.get('index'),
                    'thumb_path':    raw_thumb,
                    'thumb':         None,
                    'type':          media_type,
                    'user':          account.get('title', 'Unknown'),
                    'state':         state,
                    'progress':      progress,
                    'source':        'webhook',
                }

                with _wh_lock:
                    _wh_session     = wh_session_data
                    _wh_last_played = wh_session_data
                    _wh_updated_at  = now

                logger.info(
                    f"[Plex webhook] {event} — {wh_session_data['title']}"
                    + (f" S{wh_session_data['season']}E{wh_session_data['episode']}"
                       if wh_session_data.get('season') else '')
                    + f" ({wh_session_data['user']}) {progress}%"
                )

                # ── 2a. Polling detection: start on media.play ────────
                if event == 'media.play' and media_type == 'episode':
                    det_cfg = _get_plex_detection_cfg()
                    _play_user = account.get('title', '')
                    _allowed   = det_cfg.get('allowed_users', [])
                    if _allowed and _play_user not in _allowed:
                        logger.debug(f"[Plex] Ignoring play from user '{_play_user}' (not in allowed_users)")
                    elif det_cfg['detection_method'] == 'polling' and session_key:
                        episode_info = {
                            'series_name':    metadata.get('grandparentTitle', 'Unknown'),
                            'season_number':  metadata.get('parentIndex'),
                            'episode_number': metadata.get('index'),
                            'user_name':      account.get('title', 'Unknown'),
                            'progress_percent': progress,
                        }
                        if all([episode_info['series_name'], episode_info['season_number'] is not None,
                                episode_info['episode_number'] is not None]):
                            started = integration.start_polling(session_key, episode_info)
                            if started:
                                logger.info(
                                    f"[Plex] Polling started for "
                                    f"{episode_info['series_name']} "
                                    f"S{episode_info['season_number']}E{episode_info['episode_number']}"
                                )

            elif event == 'media.stop':
                with _wh_lock:
                    _wh_session    = None
                    _wh_updated_at = now

                # Stop any active polling for this session
                if session_key:
                    integration.stop_polling(session_key)

                # ── 2b. Stop + Threshold detection ───────────────────
                if media_type == 'episode':
                    det_cfg = _get_plex_detection_cfg()
                    if det_cfg['detection_method'] == 'stop_threshold':
                        view_offset = int(metadata.get('viewOffset', 0))
                        duration    = int(metadata.get('duration', 0))
                        progress    = round((view_offset / duration) * 100, 1) if duration else 0.0
                        series_name = metadata.get('grandparentTitle', '')
                        season      = metadata.get('parentIndex')
                        ep_num      = metadata.get('index')
                        user        = account.get('title', 'Unknown')
                        threshold   = det_cfg['progress_threshold']

                        logger.info(
                            f"[Plex webhook] media.stop — clearing display session "
                            f"({series_name} S{season}E{ep_num} {progress}%)"
                        )

                        _allowed = det_cfg.get('allowed_users', [])
                        if _allowed and user not in _allowed:
                            logger.debug(f"[Plex] Stop ignored — user '{user}' not in allowed_users")
                        elif series_name and season is not None and ep_num is not None:
                            if progress >= threshold:
                                ep_key = _ep_key(series_name, season, ep_num)
                                _mark_episode_processed(ep_key)
                                logger.info(
                                    f"[Plex] Stop+Threshold: {series_name} S{season}E{ep_num} "
                                    f"at {progress}% >= {threshold}% ({user})"
                                )
                                episode_info = {
                                    'series_name':    series_name,
                                    'season_number':  season,
                                    'episode_number': ep_num,
                                    'user_name':      user,
                                    'progress_percent': progress,
                                }
                                threading.Thread(
                                    target=integration.process_episode,
                                    args=(episode_info,),
                                    daemon=True,
                                    name="PlexStopThreshold",
                                ).start()
                            else:
                                logger.debug(
                                    f"[Plex] Stop below threshold: {series_name} S{season}E{ep_num} "
                                    f"{progress}% < {threshold}% — not processed"
                                )
                    else:
                        logger.info(f"[Plex webhook] media.stop — clearing display session")

            # ── 2c. Scrobble detection ────────────────────────────────
            elif event == 'media.scrobble' and media_type == 'episode':
                det_cfg     = _get_plex_detection_cfg()
                series_name = metadata.get('grandparentTitle', '')
                season      = metadata.get('parentIndex')
                ep_num      = metadata.get('index')
                user        = account.get('title', 'Unknown')
                method      = det_cfg['detection_method']

                _allowed = det_cfg.get('allowed_users', [])
                if _allowed and user not in _allowed:
                    logger.debug(f"[Plex] Ignoring scrobble from user '{user}' (not in allowed_users)")
                elif not (series_name and season is not None and ep_num is not None):
                    logger.warning("[Plex] Scrobble event missing episode metadata")
                elif method == 'scrobble':
                    logger.info(f"[Plex] Scrobble: {series_name} S{season}E{ep_num} ({user})")
                    episode_info = {
                        'series_name':    series_name,
                        'season_number':  season,
                        'episode_number': ep_num,
                        'user_name':      user,
                        'progress_percent': 90.0,
                    }
                    threading.Thread(
                        target=integration.process_episode,
                        args=(episode_info,),
                        daemon=True,
                        name="PlexScrobble",
                    ).start()
                elif method == 'stop_threshold':
                    # Scrobble fires as safety net — only process if stop didn't already catch it
                    ep_key = _ep_key(series_name, season, ep_num)
                    if _was_episode_processed(ep_key):
                        logger.info(
                            f"[Plex] Already processed: {series_name} S{season}E{ep_num} — skipping scrobble"
                        )
                    else:
                        logger.info(
                            f"[Plex] Scrobble (safety net): {series_name} S{season}E{ep_num} ({user})"
                        )
                        _mark_episode_processed(ep_key)
                        episode_info = {
                            'series_name':    series_name,
                            'season_number':  season,
                            'episode_number': ep_num,
                            'user_name':      user,
                            'progress_percent': 90.0,
                        }
                        threading.Thread(
                            target=integration.process_episode,
                            args=(episode_info,),
                            daemon=True,
                            name="PlexScrobbleSafetyNet",
                        ).start()
                else:
                    logger.debug(
                        f"[Plex] Scrobble ignored — detection_method is '{method}'"
                    )

            else:
                logger.debug(f"[Plex webhook] Unhandled event: {event!r}")

            return jsonify({'status': 'ok'}), 200

        # ── Now Playing widget ────────────────────────────────────────
        @bp.route('/widget')
        def widget():
            """Return Now Playing widget HTML.

            Priority:
              1. Webhook state (if updated within _WEBHOOK_STALE_SECONDS) — instant
              2. Poll /status/sessions via get_dashboard_stats() — fallback
            """
            try:
                from settings_db import get_service

                plex_config = get_service('plex')

                if not plex_config or not plex_config.get('enabled'):
                    return jsonify({'success': False, 'message': 'Plex not enabled'})

                url     = plex_config.get('url', '').rstrip('/')
                api_key = plex_config.get('api_key', '')

                if not url or not api_key:
                    return jsonify({'success': False, 'message': 'Plex not configured'})

                # ── 1. Try webhook state first ────────────────────────
                with _wh_lock:
                    wh_session    = _wh_session
                    wh_updated_at = _wh_updated_at

                webhook_fresh = (
                    wh_updated_at is not None
                    and (datetime.now() - wh_updated_at).total_seconds() < _WEBHOOK_STALE_SECONDS
                )

                if webhook_fresh:
                    now_playing = wh_session  # may be None (stopped)
                    # Resolve the relative thumb path to a full URL
                    if now_playing and now_playing.get('thumb_path'):
                        now_playing = {
                            **now_playing,
                            'thumb': f"{url}{now_playing['thumb_path']}?X-Plex-Token={api_key}",
                        }
                else:
                    # ── 2. Fall back to polling /status/sessions ──────
                    try:
                        import xml.etree.ElementTree as ET
                        resp = requests.get(
                            f"{url}/status/sessions",
                            headers={'X-Plex-Token': api_key},
                            timeout=5,
                        )
                        now_playing = None
                        if resp.ok and resp.text:
                            root  = ET.fromstring(resp.text)
                            video = root.find('.//Video')
                            if video is not None:
                                player_el = video.find('Player')
                                user_el   = video.find('User')
                                view_offset = int(video.get('viewOffset', 0))
                                duration    = int(video.get('duration', 1))
                                progress    = int((view_offset / duration) * 100) if duration else 0
                                thumb = video.get('thumb')
                                now_playing = {
                                    'title':         video.get('grandparentTitle') or video.get('title'),
                                    'episode_title': video.get('title') if video.get('grandparentTitle') else None,
                                    'season':        video.get('parentIndex'),
                                    'episode':       video.get('index'),
                                    'thumb':         f"{url}{thumb}?X-Plex-Token={api_key}" if thumb else None,
                                    'user':          user_el.get('title') if user_el is not None else 'Unknown',
                                    'state':         player_el.get('state') if player_el is not None else 'playing',
                                    'progress':      progress,
                                    'source':        'poll',
                                }
                    except Exception as poll_err:
                        logger.warning(f"[Plex widget] Polling fallback failed: {poll_err}")
                        now_playing = None

                # ── Render HTML ───────────────────────────────────────
                if not now_playing:
                    html = (
                        '<div class="d-flex align-items-center gap-2 px-2 py-1 rounded"'
                        ' style="background:rgba(255,255,255,0.04); min-height:36px;">'
                        '<img src="https://www.plex.tv/wp-content/themes/plex/assets/img/plex-logo.svg"'
                        ' style="width:16px;height:16px;opacity:0.6;flex-shrink:0;">'
                        '<i class="fas fa-tv text-muted" style="font-size:11px;opacity:0.4;"></i>'
                        '<span class="text-muted" style="font-size:12px;">Nothing playing</span>'
                        '</div>'
                    )
                else:
                    thumb_html = (
                        f'<img src="{now_playing["thumb"]}" class="rounded"'
                        f' style="width:36px;height:36px;object-fit:cover;flex-shrink:0;">'
                        if now_playing.get('thumb') else ''
                    )

                    if now_playing.get('episode_title') and now_playing.get('season') and now_playing.get('episode'):
                        title    = now_playing['title']
                        subtitle = f"S{now_playing['season']}E{now_playing['episode']} · {now_playing['episode_title']}"
                    else:
                        title    = now_playing['title']
                        subtitle = now_playing.get('user', 'Unknown User')

                    state_icon = 'play' if now_playing.get('state') == 'playing' else 'pause'
                    progress   = now_playing.get('progress', 0)

                    html = (
                        f'<div class="d-flex align-items-center gap-2 px-2 py-1 rounded"'
                        f' style="background:rgba(255,255,255,0.04); min-height:36px;">'
                        f'<img src="https://www.plex.tv/wp-content/themes/plex/assets/img/plex-logo.svg"'
                        f' style="width:16px;height:16px;flex-shrink:0;">'
                        f'{thumb_html}'
                        f'<div class="flex-grow-1 overflow-hidden">'
                        f'<div class="text-truncate fw-semibold" style="font-size:12px;line-height:1.2;">{title}</div>'
                        f'<div class="text-truncate text-muted" style="font-size:11px;line-height:1.2;">{subtitle}</div>'
                        f'</div>'
                        f'<span class="badge bg-warning text-dark flex-shrink-0" style="font-size:10px;">'
                        f'<i class="fas fa-{state_icon} me-1"></i>{progress}%'
                        f'</span>'
                        f'</div>'
                    )

                return jsonify({'success': True, 'html': html})

            except Exception as e:
                logger.error(f"Error generating Plex widget: {e}")
                return jsonify({'success': False, 'message': str(e)})
        
        @bp.route('/watchlist')
        def watchlist():
            """Return watchlist section HTML with status badges on posters"""
            try:
                from settings_db import get_service
                
                plex_config = get_service('plex')
                
                if not plex_config or not plex_config.get('enabled'):
                    return jsonify({'success': False, 'message': 'Plex not enabled'})
                
                api_key = plex_config.get('api_key')
                
                if not api_key:
                    return jsonify({'success': False, 'message': 'Plex not configured'})
                
                # Use enriched watchlist with status
                watchlist_items = integration.get_watchlist_with_status(api_key)

                # Compute sync status text (always needed for card header)
                sync_data = load_sync_data()
                sync_config = integration.get_sync_config()
                last_sync = sync_data.get('last_full_sync')
                sync_enabled = sync_config.get('enabled', False)

                if sync_enabled and last_sync:
                    try:
                        sync_dt = datetime.fromisoformat(last_sync)
                        ago_seconds = (datetime.now() - sync_dt).total_seconds()
                        if ago_seconds < 3600:
                            ago_text = f"{int(ago_seconds / 60)}m ago"
                        elif ago_seconds < 86400:
                            ago_text = f"{int(ago_seconds / 3600)}h ago"
                        else:
                            ago_text = f"{int(ago_seconds / 86400)}d ago"
                    except:
                        ago_text = "unknown"
                    sync_text = f"Synced {ago_text}"
                elif sync_enabled:
                    sync_text = "Sync pending..."
                else:
                    sync_text = "Auto-sync off"

                if not watchlist_items:
                    html = '<p class="text-muted text-center py-4">Your watchlist is empty</p>'
                else:
                    # Sync info already computed above
                    
                    items_html = ''
                    for item in watchlist_items:
                        thumb = item.get('thumb') or '/static/placeholder-poster.png'
                        title = item.get('title', 'Unknown')
                        year = f" ({item.get('year')})" if item.get('year') else ''
                        media_type = item.get('type', 'movie')
                        type_icon = 'fa-tv' if media_type == 'show' else 'fa-film'
                        
                        # Status badge
                        status = item.get('sync_status', 'on_watchlist')
                        label = item.get('status_label', '')
                        color = item.get('status_color', '#6c757d')
                        
                        # Status icon mapping
                        status_icons = {
                            'on_watchlist': 'fa-bookmark',
                            'pending': 'fa-user-clock',
                            'requested': 'fa-clock',
                            'downloading': 'fa-download',
                            'available': 'fa-check-circle',
                            'watched': 'fa-eye',
                            'error': 'fa-exclamation-triangle',
                        }
                        status_icon = status_icons.get(status, 'fa-bookmark')
                        
                        items_html += f'''
                        <div class="watchlist-item" data-status="{status}" data-type="{media_type}">
                            <div class="watchlist-poster-wrap">
                                <img src="{thumb}" class="watchlist-poster" alt="{title}">
                                <span class="watchlist-type-badge">
                                    <i class="fas {type_icon}"></i>
                                </span>
                                <span class="watchlist-status-badge" style="background: {color};">
                                    <i class="fas {status_icon}" style="font-size: 9px;"></i> {label}
                                </span>
                            </div>
                            <div class="watchlist-title">{title}{year}</div>
                        </div>
                        '''
                    
                    html = f'''
                    <div class="watchlist-container">
                        <div class="watchlist-scroll">
                            {items_html}
                        </div>
                    </div>
                    <style>
                    .watchlist-container {{
                        margin-top: 0;
                    }}
                    .watchlist-scroll {{
                        display: flex;
                        gap: 12px;
                        overflow-x: auto;
                        padding: 8px 0;
                        -webkit-overflow-scrolling: touch;
                    }}
                    .watchlist-item {{
                        flex: 0 0 120px;
                        text-align: center;
                    }}
                    .watchlist-poster-wrap {{
                        position: relative;
                        display: inline-block;
                    }}
                    .watchlist-poster {{
                        width: 120px;
                        height: 180px;
                        object-fit: cover;
                        border-radius: 8px;
                        box-shadow: 0 2px 8px rgba(0,0,0,0.3);
                        transition: transform 0.2s;
                    }}
                    .watchlist-poster:hover {{
                        transform: scale(1.05);
                        cursor: pointer;
                    }}
                    .watchlist-type-badge {{
                        position: absolute;
                        top: 6px;
                        left: 6px;
                        background: rgba(0,0,0,0.7);
                        color: #fff;
                        padding: 2px 6px;
                        border-radius: 4px;
                        font-size: 10px;
                    }}
                    .watchlist-status-badge {{
                        position: absolute;
                        bottom: 6px;
                        left: 50%;
                        transform: translateX(-50%);
                        color: #fff;
                        padding: 2px 8px;
                        border-radius: 10px;
                        font-size: 10px;
                        white-space: nowrap;
                        font-weight: 600;
                        letter-spacing: 0.3px;
                    }}
                    .watchlist-title {{
                        font-size: 12px;
                        margin-top: 8px;
                        overflow: hidden;
                        text-overflow: ellipsis;
                        white-space: nowrap;
                    }}
                    @media (max-width: 768px) {{
                        .watchlist-container {{
                            max-width: 100vw;
                            overflow: hidden;
                        }}
                        .watchlist-scroll {{
                            max-width: calc(100vw - 48px);
                        }}
                        .watchlist-item {{
                            flex: 0 0 calc(50% - 6px);
                        }}
                        .watchlist-poster {{
                            width: 100%;
                            height: 150px;
                        }}
                        .watchlist-title {{
                            font-size: 11px;
                        }}
                    }}
                    </style>
                    '''
                
                return jsonify({'success': True, 'html': html, 'sync_text': sync_text})

            except Exception as e:
                logger.error(f"Error generating watchlist: {e}")
                return jsonify({'success': False, 'message': str(e)})
        
        # ==========================================
        # Sync API Routes
        # ==========================================
        
        @bp.route('/sync', methods=['POST'])
        def sync_now():
            """Manual sync trigger"""
            try:
                result = integration.sync_watchlist()
                return jsonify(result)
            except Exception as e:
                logger.error(f"Manual sync error: {e}")
                return jsonify({'success': False, 'message': str(e)}), 500
        
        @bp.route('/sync/status')
        def sync_status():
            """Get current sync status and history"""
            try:
                sync_data = load_sync_data()
                sync_config = integration.get_sync_config()
                return jsonify({
                    'success': True,
                    'enabled': sync_config.get('enabled', False),
                    'interval_minutes': sync_config.get('interval_minutes', 120),
                    'last_sync': sync_data.get('last_full_sync'),
                    'stats': sync_data.get('stats', {}),
                    'synced_count': len(sync_data.get('synced_items', {})),
                    'scheduler_running': integration._sync_running
                })
            except Exception as e:
                return jsonify({'success': False, 'message': str(e)})
        
        @bp.route('/sync/config', methods=['GET', 'POST'])
        def sync_config_endpoint():
            """Get or update watchlist sync configuration"""
            try:
                from settings_db import get_service, save_service
                
                if request.method == 'GET':
                    return jsonify({
                        'success': True,
                        'config': integration.get_sync_config()
                    })
                
                # POST - update config
                data = request.json or {}
                plex_config = get_service('plex') or {}

                # Merge new sync config - watchlist_sync lives inside the config JSON field
                service_config = plex_config.get('config') or {}
                current_sync = service_config.get('watchlist_sync', {})

                # Only update provided fields
                for key in ('enabled', 'interval_minutes',
                           'tv_quality_profile', 'tv_root_folder',
                           'movie_quality_profile', 'movie_root_folder',
                           'auto_remove_watched'):
                    if key in data:
                        current_sync[key] = data[key]

                # Movie cleanup sub-config
                if 'movie_cleanup' in data:
                    current_sync['movie_cleanup'] = {
                        **current_sync.get('movie_cleanup', {}),
                        **data['movie_cleanup']
                    }

                service_config['watchlist_sync'] = current_sync
                save_service(
                    service_type='plex',
                    name=plex_config.get('name', 'default'),
                    url=plex_config.get('url', ''),
                    api_key=plex_config.get('api_key'),
                    config=service_config,
                    enabled=plex_config.get('enabled', True)
                )
                
                # Start/stop scheduler based on enabled state
                if current_sync.get('enabled') and not integration._sync_running:
                    integration.start_sync_scheduler()
                elif not current_sync.get('enabled') and integration._sync_running:
                    integration.stop_sync_scheduler()
                
                return jsonify({'success': True, 'config': current_sync})
                
            except Exception as e:
                logger.error(f"Sync config error: {e}")
                return jsonify({'success': False, 'message': str(e)}), 500
        
        @bp.route('/sync/items')
        def sync_items():
            """Get list of all synced items and their status"""
            try:
                sync_data = load_sync_data()
                items = list(sync_data.get('synced_items', {}).values())
                # Sort by sync date descending
                items.sort(key=lambda x: x.get('synced_at', ''), reverse=True)
                return jsonify({'success': True, 'items': items})
            except Exception as e:
                return jsonify({'success': False, 'message': str(e)})
        
        @bp.route('/mark-watched', methods=['POST'])
        def mark_watched():
            """Manually mark an item as watched (or called from Tautulli hook)"""
            try:
                data = request.json or {}
                tmdb_id = data.get('tmdb_id')
                media_type = data.get('type', 'movie')
                
                if not tmdb_id:
                    return jsonify({'success': False, 'message': 'tmdb_id required'}), 400
                
                integration.mark_item_watched(tmdb_id, media_type)
                return jsonify({'success': True})
            except Exception as e:
                return jsonify({'success': False, 'message': str(e)}), 500
        
        @bp.route('/cleanup', methods=['POST'])
        def run_cleanup():
            """Manually trigger movie cleanup"""
            try:
                result = integration.cleanup_watched_movies()
                return jsonify({'success': True, **result})
            except Exception as e:
                return jsonify({'success': False, 'message': str(e)}), 500
        
        return bp

# Export integration instance
integration = PlexIntegration()
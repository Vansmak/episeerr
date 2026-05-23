"""
Trakt Integration for Episeerr

Provides:
  - OAuth token management (device code flow, auto-refresh)
  - Watchlist sync: fetch shows/movies from Trakt and add to Sonarr/Radarr
  - Dashboard section showing watchlist status
"""

import os
import json
import logging
import threading
import time
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from episeerr_utils import http
from integrations.base import ServiceIntegration

logger = logging.getLogger(__name__)

TRAKT_API_BASE = 'https://api.trakt.tv'

# ──────────────────────────────────────────────────────────────────
#  Sync data (data/trakt_sync.json)
# ──────────────────────────────────────────────────────────────────

SYNC_DATA_FILE = os.path.join(os.getcwd(), 'data', 'trakt_sync.json')


def _load_sync_data() -> dict:
    try:
        if os.path.exists(SYNC_DATA_FILE):
            with open(SYNC_DATA_FILE, 'r') as fh:
                return json.load(fh)
    except Exception as exc:
        logger.error(f"[Trakt] Error loading sync data: {exc}")
    return {
        'synced_items': {},
        'last_full_sync': None,
        'stats': {'total_synced_tv': 0, 'total_synced_movies': 0}
    }


def _save_sync_data(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SYNC_DATA_FILE), exist_ok=True)
        with open(SYNC_DATA_FILE, 'w') as fh:
            json.dump(data, fh, indent=2, default=str)
    except Exception as exc:
        logger.error(f"[Trakt] Error saving sync data: {exc}")


# ──────────────────────────────────────────────────────────────────
#  Integration class
# ──────────────────────────────────────────────────────────────────

class TraktIntegration(ServiceIntegration):

    _sync_thread: Optional[threading.Thread] = None
    _sync_running: bool = False

    # ── Metadata ──────────────────────────────────────────────────

    @property
    def service_name(self) -> str:
        return 'trakt'

    @property
    def display_name(self) -> str:
        return 'Trakt'

    @property
    def description(self) -> str:
        return 'Trakt watchlist sync — add shows/movies to Sonarr/Radarr automatically'

    @property
    def icon(self) -> str:
        return 'https://walter.trakt.tv/hotlink-ok/public/favicon.svg'

    @property
    def category(self) -> str:
        return 'media'

    @property
    def default_port(self) -> int:
        return 443

    # ── Connection / Stats ────────────────────────────────────────

    def test_connection(self, url: str, api_key: str) -> Tuple[bool, str]:
        if not api_key:
            return False, 'Client ID not configured'
        try:
            cfg = self._get_trakt_config()
            if not cfg or not cfg.get('access_token'):
                return False, 'Not authenticated — add Client ID/Secret and authenticate'
            headers = self._build_headers(cfg)
            resp = http.get(f"{TRAKT_API_BASE}/users/me/watchlist/shows",
                            headers=headers, timeout=10)
            if resp.ok:
                return True, f"Connected as {cfg.get('username', 'unknown')}"
            if resp.status_code == 401:
                return False, 'Token expired — re-authenticate'
            return False, f"HTTP {resp.status_code}"
        except Exception as exc:
            return False, str(exc)

    def get_dashboard_stats(self, url: str, api_key: str) -> Dict[str, Any]:
        ok, msg = self.test_connection(url, api_key)
        return {'connected': ok, 'message': msg}

    # ── Setup fields ──────────────────────────────────────────────

    def get_setup_fields(self) -> Optional[List[Dict]]:
        return [
            {
                'name': 'api_key',
                'label': 'Client ID',
                'type': 'text',
                'placeholder': 'Trakt application client_id',
                'required': True,
                'help_text': 'From https://trakt.tv/oauth/applications → your app → Client ID'
            }
        ]

    def get_custom_setup_html(self, saved_values: dict = None) -> str:
        saved_values = saved_values or {}
        client_secret = saved_values.get('client_secret', '')
        access_token = saved_values.get('access_token', '')
        refresh_token = saved_values.get('refresh_token', '')
        sync_interval = saved_values.get('sync_interval_minutes', 60)
        sync_enabled = saved_values.get('sync_enabled', False)
        expires_at = saved_values.get('expires_at')

        enabled_checked = 'checked' if sync_enabled else ''
        token_status_html = ''
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                if exp > now:
                    days = (exp - now).days
                    token_status_html = (
                        f'<span class="badge bg-success">Valid — expires in {days} day(s)</span>'
                    )
                else:
                    token_status_html = '<span class="badge bg-danger">Expired</span>'
            except Exception:
                token_status_html = '<span class="badge bg-secondary">Unknown</span>'
        elif access_token:
            token_status_html = '<span class="badge bg-warning text-dark">No expiry info</span>'
        else:
            token_status_html = '<span class="badge bg-secondary">Not authenticated</span>'

        interval_options = ''.join(
            f'<option value="{v}" {"selected" if v == sync_interval else ""}>{lbl}</option>'
            for v, lbl in [
                (15, '15 minutes'), (30, '30 minutes'), (60, '1 hour'),
                (120, '2 hours'), (360, '6 hours'), (720, '12 hours'), (1440, '24 hours')
            ]
        )

        return f'''
        <!-- ── OAuth Tokens ────────────────────────────────── -->
        <div style="border-top:1px solid rgba(255,255,255,0.1);margin-top:20px;padding-top:20px;">
            <h6 class="mb-3">
                <i class="fas fa-key text-warning me-2"></i>OAuth Tokens
            </h6>
            <div class="row">
                <div class="col-md-8 mb-3">
                    <label class="form-label">Client Secret</label>
                    <input type="password" class="form-control form-control-sm"
                           name="trakt-client-secret" value="{client_secret}"
                           placeholder="Client secret from Trakt app settings">
                </div>
            </div>
            <div class="row">
                <div class="col-md-8 mb-3">
                    <label class="form-label">Access Token</label>
                    <input type="password" class="form-control form-control-sm"
                           name="trakt-access-token" value="{access_token}"
                           placeholder="OAuth access token">
                </div>
            </div>
            <div class="row">
                <div class="col-md-8 mb-3">
                    <label class="form-label">Refresh Token</label>
                    <input type="password" class="form-control form-control-sm"
                           name="trakt-refresh-token" value="{refresh_token}"
                           placeholder="OAuth refresh token">
                    <div class="mt-1">Token status: {token_status_html}</div>
                </div>
            </div>
            <small class="text-muted d-block mb-2">
                Get tokens via the Trakt device code flow or a third-party tool.
                Access tokens are automatically refreshed 24 hours before expiry.
            </small>
        </div>

        <!-- ── Watchlist Auto-Sync ───────────────────────��───── -->
        <div style="border-top:1px solid rgba(255,255,255,0.1);margin-top:20px;padding-top:20px;">
            <h6 class="mb-3">
                <i class="fas fa-sync-alt text-info me-2"></i>Watchlist Auto-Sync
            </h6>
            <div class="row">
                <div class="col-md-6 mb-3">
                    <div class="form-check form-switch">
                        <input type="checkbox" class="form-check-input" id="trakt-sync-enabled"
                               name="trakt-sync-enabled" {enabled_checked}>
                        <label class="form-check-label" for="trakt-sync-enabled">Enable automatic sync</label>
                    </div>
                    <small class="text-muted">
                        Periodically sync Trakt watchlist → Sonarr/Radarr
                    </small>
                </div>
                <div class="col-md-6 mb-3">
                    <label class="form-label">Sync Interval</label>
                    <select class="form-select form-select-sm" name="trakt-sync-interval">
                        {interval_options}
                    </select>
                </div>
            </div>
            <div class="row">
                <div class="col-md-4">
                    <button type="button" class="btn btn-sm btn-outline-info"
                            onclick="triggerTraktSync()">
                        <i class="fas fa-sync-alt me-1"></i>Sync Now
                    </button>
                </div>
            </div>
        </div>

        <script>
        function triggerTraktSync() {{
            fetch('/api/integration/trakt/sync', {{method: 'POST'}})
                .then(r => r.json())
                .then(d => alert(d.message || 'Sync started'))
                .catch(e => alert('Error: ' + e));
        }}
        </script>
        '''

    def preprocess_save_data(self, normalized_data: dict) -> None:
        from settings_db import get_service
        existing_cfg = (get_service('trakt', 'default') or {}).get('config') or {}

        normalized_data['client_secret'] = normalized_data.pop(
            'client-secret', existing_cfg.get('client_secret', '')
        )
        normalized_data['access_token'] = normalized_data.pop(
            'trakt-access-token', normalized_data.pop(
                'access-token', existing_cfg.get('access_token', '')
            )
        )
        normalized_data['refresh_token'] = normalized_data.pop(
            'trakt-refresh-token', normalized_data.pop(
                'refresh-token', existing_cfg.get('refresh_token', '')
            )
        )
        # Preserve programmatically-set expires_at
        normalized_data['expires_at'] = normalized_data.pop(
            'expires-at', existing_cfg.get('expires_at')
        )
        normalized_data['sync_enabled'] = normalized_data.pop(
            'sync-enabled', existing_cfg.get('sync_enabled', False)
        )
        normalized_data['sync_interval_minutes'] = int(
            normalized_data.pop('trakt-sync-interval', normalized_data.pop(
                'sync-interval', existing_cfg.get('sync_interval_minutes', 60)
            )) or 60
        )

    def on_after_save(self, normalized_data: dict) -> None:
        if normalized_data.get('sync_enabled') and not self._sync_running:
            self.start_sync_scheduler()
        elif not normalized_data.get('sync_enabled') and self._sync_running:
            self.stop_sync_scheduler()

    # ── Config / token helpers ────────────────────────────────────

    def _get_trakt_config(self) -> Optional[dict]:
        try:
            from settings_db import get_service
            svc = get_service('trakt', 'default')
            if not svc:
                return None
            cfg = svc.get('config') or {}
            return {
                'client_id': svc.get('api_key', ''),
                'client_secret': cfg.get('client_secret', ''),
                'access_token': cfg.get('access_token', ''),
                'refresh_token': cfg.get('refresh_token', ''),
                'expires_at': cfg.get('expires_at'),
                'sync_enabled': cfg.get('sync_enabled', False),
                'sync_interval_minutes': int(cfg.get('sync_interval_minutes', 60)),
            }
        except Exception as exc:
            logger.error(f"[Trakt] Could not load config: {exc}")
        return None

    def _build_headers(self, cfg: dict) -> dict:
        return {
            'trakt-api-key': cfg.get('client_id', ''),
            'trakt-api-version': '2',
            'Authorization': f"Bearer {cfg.get('access_token', '')}",
            'Content-Type': 'application/json',
        }

    def _maybe_refresh_token(self, cfg: dict) -> dict:
        """Refresh access token if expired or within 24 hours of expiry."""
        expires_at = cfg.get('expires_at')
        if not expires_at or not cfg.get('refresh_token') or not cfg.get('client_id'):
            return cfg
        try:
            exp = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            if (exp - now).total_seconds() > 86400:
                return cfg  # more than 24h remaining — no refresh needed

            logger.info("[Trakt] Refreshing access token …")
            resp = http.post(f"{TRAKT_API_BASE}/oauth/token", json={
                'refresh_token': cfg['refresh_token'],
                'client_id': cfg['client_id'],
                'client_secret': cfg.get('client_secret', ''),
                'grant_type': 'refresh_token',
            }, timeout=15)

            if not resp.ok:
                logger.error(f"[Trakt] Token refresh failed: {resp.status_code}")
                return cfg

            token_data = resp.json()
            new_access = token_data.get('access_token', cfg['access_token'])
            new_refresh = token_data.get('refresh_token', cfg['refresh_token'])
            expires_in = token_data.get('expires_in', 7776000)
            new_expires = datetime.now(timezone.utc).timestamp() + expires_in
            new_expires_iso = datetime.fromtimestamp(new_expires, tz=timezone.utc).isoformat()

            # Persist refreshed tokens
            try:
                from settings_db import get_service, save_service
                svc = get_service('trakt', 'default') or {}
                existing_cfg = svc.get('config') or {}
                existing_cfg.update({
                    'access_token': new_access,
                    'refresh_token': new_refresh,
                    'expires_at': new_expires_iso,
                })
                save_service(
                    service_type='trakt',
                    name='default',
                    url=svc.get('url', ''),
                    api_key=svc.get('api_key', cfg['client_id']),
                    config=existing_cfg
                )
            except Exception as exc:
                logger.error(f"[Trakt] Could not persist refreshed token: {exc}")

            cfg = dict(cfg)
            cfg['access_token'] = new_access
            cfg['refresh_token'] = new_refresh
            cfg['expires_at'] = new_expires_iso
            logger.info("[Trakt] Token refreshed successfully")
            return cfg

        except Exception as exc:
            logger.error(f"[Trakt] Token refresh error: {exc}", exc_info=True)
        return cfg

    # ── Trakt API calls ───────────────────────────────────────────

    def _api_get(self, path: str) -> Optional[list]:
        cfg = self._get_trakt_config()
        if not cfg or not cfg.get('access_token'):
            return None
        cfg = self._maybe_refresh_token(cfg)
        try:
            resp = http.get(f"{TRAKT_API_BASE}{path}",
                            headers=self._build_headers(cfg), timeout=15)
            if resp.ok:
                return resp.json()
            logger.error(f"[Trakt] GET {path} → {resp.status_code}")
        except Exception as exc:
            logger.error(f"[Trakt] GET {path} error: {exc}")
        return None

    def fetch_watchlist_shows(self) -> List[dict]:
        data = self._api_get('/users/me/watchlist/shows')
        if not data:
            return []
        results = []
        for entry in data:
            show = entry.get('show', {})
            ids = show.get('ids', {})
            results.append({
                'title': show.get('title', 'Unknown'),
                'tmdb_id': ids.get('tmdb'),
                'tvdb_id': ids.get('tvdb'),
                'trakt_id': ids.get('trakt'),
                'year': show.get('year'),
                'media_type': 'show',
            })
        return results

    def fetch_watchlist_movies(self) -> List[dict]:
        data = self._api_get('/users/me/watchlist/movies')
        if not data:
            return []
        results = []
        for entry in data:
            movie = entry.get('movie', {})
            ids = movie.get('ids', {})
            results.append({
                'title': movie.get('title', 'Unknown'),
                'tmdb_id': ids.get('tmdb'),
                'trakt_id': ids.get('trakt'),
                'year': movie.get('year'),
                'media_type': 'movie',
            })
        return results

    # ── Sonarr / Radarr helpers (same pattern as arvio.py) ────────

    def _check_sonarr(self, tmdb_id) -> Optional[dict]:
        try:
            import sonarr_utils
            prefs = sonarr_utils.load_preferences()
            headers = {'X-Api-Key': prefs['SONARR_API_KEY']}
            resp = http.get(f"{prefs['SONARR_URL']}/api/v3/series", headers=headers, timeout=10)
            if resp.ok:
                for s in resp.json():
                    if str(s.get('tmdbId')) == str(tmdb_id):
                        return s
        except Exception as exc:
            logger.error(f"[Trakt] Sonarr check error: {exc}")
        return None

    def _check_radarr(self, tmdb_id) -> Optional[dict]:
        try:
            from settings_db import get_service
            rc = get_service('radarr') or {}
            url, key = rc.get('url', '').rstrip('/'), rc.get('api_key', '')
            if not url or not key:
                return None
            headers = {'X-Api-Key': key}
            resp = http.get(f"{url}/api/v3/movie", headers=headers, timeout=10)
            if resp.ok:
                for m in resp.json():
                    if str(m.get('tmdbId')) == str(tmdb_id):
                        return m
        except Exception as exc:
            logger.error(f"[Trakt] Radarr check error: {exc}")
        return None

    def _add_tv_to_sonarr(self, item: dict) -> dict:
        try:
            import sonarr_utils
            prefs = sonarr_utils.load_preferences()
            headers = {'X-Api-Key': prefs['SONARR_API_KEY'], 'Content-Type': 'application/json'}
            sonarr_url = prefs['SONARR_URL']
            tmdb_id = item.get('tmdb_id')
            tvdb_id = item.get('tvdb_id')

            if not tmdb_id and not tvdb_id:
                return {'success': False, 'status': 'missing_id',
                        'message': f"No TMDB/TVDB ID for {item.get('title')}"}

            if self._check_sonarr(tmdb_id):
                return {'success': True, 'status': 'already_exists',
                        'message': f"{item.get('title')} already in Sonarr"}

            term = f"tvdb:{tvdb_id}" if tvdb_id else f"tmdb:{tmdb_id}"
            resp = http.get(f"{sonarr_url}/api/v3/series/lookup?term={term}",
                            headers=headers, timeout=10)
            if not resp.ok or not resp.json():
                return {'success': False, 'status': 'lookup_failed',
                        'message': f"Could not find {item.get('title')} in Sonarr"}

            series_data = resp.json()[0] if isinstance(resp.json(), list) else resp.json()

            qp_resp = http.get(f"{sonarr_url}/api/v3/qualityprofile", headers=headers, timeout=10)
            quality_profile = qp_resp.json()[0]['id'] if qp_resp.ok and qp_resp.json() else 1

            rf_resp = http.get(f"{sonarr_url}/api/v3/rootfolder", headers=headers, timeout=10)
            root_folder = rf_resp.json()[0]['path'] if rf_resp.ok and rf_resp.json() else '/tv'

            tags = []
            tag_resp = http.get(f"{sonarr_url}/api/v3/tag", headers=headers, timeout=10)
            if tag_resp.ok:
                existing_tags = {t['label'].lower(): t['id'] for t in tag_resp.json()}
                if 'episeerr_select' in existing_tags:
                    tags.append(existing_tags['episeerr_select'])
                else:
                    cr = http.post(f"{sonarr_url}/api/v3/tag", headers=headers,
                                   json={'label': 'episeerr_select'}, timeout=10)
                    if cr.ok:
                        tags.append(cr.json()['id'])

            add_resp = http.post(f"{sonarr_url}/api/v3/series", headers=headers, timeout=15, json={
                'tvdbId': series_data.get('tvdbId'),
                'title': series_data.get('title'),
                'qualityProfileId': quality_profile,
                'rootFolderPath': root_folder,
                'monitored': True,
                'seasonFolder': True,
                'tags': tags,
                'addOptions': {'monitor': 'none', 'searchForMissingEpisodes': False}
            })

            if add_resp.ok:
                sid = add_resp.json().get('id')
                return {'success': True, 'status': 'added', 'series_id': sid,
                        'message': f"Added {item.get('title')} — pending selection"}
            if add_resp.status_code == 400 and 'already been added' in add_resp.text.lower():
                return {'success': True, 'status': 'already_exists',
                        'message': f"{item.get('title')} already in Sonarr"}
            return {'success': False, 'status': 'add_failed',
                    'message': f"Sonarr add failed ({add_resp.status_code})"}

        except Exception as exc:
            logger.error(f"[Trakt] _add_tv_to_sonarr error: {exc}")
            return {'success': False, 'status': 'error', 'message': str(exc)}

    def _add_movie_to_radarr(self, item: dict) -> dict:
        try:
            from settings_db import get_service
            rc = get_service('radarr') or {}
            radarr_url = rc.get('url', '').rstrip('/')
            radarr_key = rc.get('api_key', '')
            if not radarr_url or not radarr_key:
                return {'success': False, 'status': 'not_configured',
                        'message': 'Radarr not configured'}

            headers = {'X-Api-Key': radarr_key, 'Content-Type': 'application/json'}
            tmdb_id = item.get('tmdb_id')
            if not tmdb_id:
                return {'success': False, 'status': 'missing_id',
                        'message': f"No TMDB ID for {item.get('title')}"}

            if self._check_radarr(tmdb_id):
                return {'success': True, 'status': 'already_exists',
                        'message': f"{item.get('title')} already in Radarr"}

            lkp = http.get(f"{radarr_url}/api/v3/movie/lookup/tmdb?tmdbId={tmdb_id}",
                           headers=headers, timeout=10)
            if not lkp.ok:
                return {'success': False, 'status': 'lookup_failed',
                        'message': f"Radarr lookup failed for {item.get('title')}"}

            movie_data = lkp.json()

            qp_resp = http.get(f"{radarr_url}/api/v3/qualityprofile", headers=headers, timeout=10)
            quality_profile = qp_resp.json()[0]['id'] if qp_resp.ok and qp_resp.json() else 1

            rf_resp = http.get(f"{radarr_url}/api/v3/rootfolder", headers=headers, timeout=10)
            root_folder = rf_resp.json()[0]['path'] if rf_resp.ok and rf_resp.json() else '/movies'

            add_resp = http.post(f"{radarr_url}/api/v3/movie", headers=headers, timeout=15, json={
                'tmdbId': int(tmdb_id),
                'title': movie_data.get('title', item.get('title')),
                'qualityProfileId': quality_profile,
                'rootFolderPath': root_folder,
                'monitored': True,
                'addOptions': {'searchForMovie': True}
            })

            if add_resp.ok:
                mid = add_resp.json().get('id')
                return {'success': True, 'status': 'added', 'movie_id': mid,
                        'message': f"Added {item.get('title')} to Radarr"}
            if add_resp.status_code == 400 and 'already been added' in add_resp.text.lower():
                return {'success': True, 'status': 'already_exists',
                        'message': f"{item.get('title')} already in Radarr"}
            return {'success': False, 'status': 'add_failed',
                    'message': f"Radarr add failed ({add_resp.status_code})"}

        except Exception as exc:
            logger.error(f"[Trakt] _add_movie_to_radarr error: {exc}")
            return {'success': False, 'status': 'error', 'message': str(exc)}

    # ── Watchlist sync ────────────────────────────────────────────

    def sync_watchlist(self) -> dict:
        cfg = self._get_trakt_config()
        if not cfg or not cfg.get('access_token'):
            return {'success': False, 'message': 'Trakt not authenticated'}

        shows = self.fetch_watchlist_shows()
        movies = self.fetch_watchlist_movies()
        all_items = shows + movies

        if not all_items:
            return {'success': True, 'message': 'Trakt watchlist empty', 'processed': 0}

        sync_data = _load_sync_data()
        results = {
            'success': True, 'processed': 0, 'skipped': 0,
            'added_tv': 0, 'added_movies': 0, 'already_exists': 0,
            'errors': 0, 'items': []
        }

        for item in all_items:
            tmdb_id = item.get('tmdb_id')
            media_type = item.get('media_type', 'show')
            item_key = f"{media_type}_{tmdb_id}"

            existing = sync_data['synced_items'].get(item_key, {})
            if existing.get('status') in ('added_to_sonarr', 'added_to_radarr',
                                          'already_exists', 'pending_selection'):
                results['skipped'] += 1
                continue

            if media_type == 'show':
                if self._check_sonarr(tmdb_id):
                    sync_data['synced_items'][item_key] = {
                        'tmdb_id': tmdb_id, 'title': item.get('title'), 'type': 'tv',
                        'synced_at': datetime.now().isoformat(), 'source': 'trakt',
                        'status': 'already_exists'
                    }
                    results['already_exists'] += 1
                    results['processed'] += 1
                    results['items'].append({'title': item.get('title'), 'type': 'show',
                                             'status': 'already_exists'})
                    continue

                result = self._add_tv_to_sonarr(item)
                status = 'added_to_sonarr' if result['success'] and result['status'] == 'added' \
                    else result['status']
                sync_data['synced_items'][item_key] = {
                    'tmdb_id': tmdb_id, 'title': item.get('title'), 'type': 'tv',
                    'synced_at': datetime.now().isoformat(), 'source': 'trakt',
                    'status': status, 'sonarr_series_id': result.get('series_id')
                }
                if result['success']:
                    results['added_tv'] += 1
                else:
                    results['errors'] += 1
                results['processed'] += 1
                results['items'].append({'title': item.get('title'), 'type': 'show',
                                         'status': status, 'message': result.get('message')})

            elif media_type == 'movie':
                if self._check_radarr(tmdb_id):
                    sync_data['synced_items'][item_key] = {
                        'tmdb_id': tmdb_id, 'title': item.get('title'), 'type': 'movie',
                        'synced_at': datetime.now().isoformat(), 'source': 'trakt',
                        'status': 'already_exists'
                    }
                    results['already_exists'] += 1
                    results['processed'] += 1
                    results['items'].append({'title': item.get('title'), 'type': 'movie',
                                             'status': 'already_exists'})
                    continue

                result = self._add_movie_to_radarr(item)
                status = 'added_to_radarr' if result['success'] and result['status'] == 'added' \
                    else result['status']
                sync_data['synced_items'][item_key] = {
                    'tmdb_id': tmdb_id, 'title': item.get('title'), 'type': 'movie',
                    'synced_at': datetime.now().isoformat(), 'source': 'trakt',
                    'status': status, 'movie_id': result.get('movie_id')
                }
                if result['success']:
                    results['added_movies'] += 1
                else:
                    results['errors'] += 1
                results['processed'] += 1
                results['items'].append({'title': item.get('title'), 'type': 'movie',
                                         'status': status, 'message': result.get('message')})

        sync_data['last_full_sync'] = datetime.now().isoformat()
        sync_data['stats']['total_synced_tv'] += results['added_tv']
        sync_data['stats']['total_synced_movies'] += results['added_movies']
        _save_sync_data(sync_data)

        logger.info(
            f"[Trakt] Sync complete: {results['processed']} processed, "
            f"{results['added_tv']} TV added, {results['added_movies']} movies added, "
            f"{results['already_exists']} already exist, {results['errors']} errors"
        )
        return results

    # ── Scheduler ─────────────────────────────────────────────────

    def start_sync_scheduler(self) -> None:
        if self._sync_running:
            return
        cfg = self._get_trakt_config()
        interval = (cfg or {}).get('sync_interval_minutes', 60)

        def _loop():
            self._sync_running = True
            time.sleep(30)
            while self._sync_running:
                try:
                    logger.info("[Trakt] Running scheduled watchlist sync …")
                    self.sync_watchlist()
                except Exception as exc:
                    logger.error(f"[Trakt] Scheduled sync error: {exc}", exc_info=True)
                try:
                    fresh = self._get_trakt_config()
                    interval = (fresh or {}).get('sync_interval_minutes', 60)
                except Exception:
                    pass
                time.sleep(interval * 60)

        self._sync_thread = threading.Thread(target=_loop, daemon=True, name='trakt-sync')
        self._sync_thread.start()
        logger.info(f"[Trakt] Sync scheduler started (every {interval} min)")

    def stop_sync_scheduler(self) -> None:
        self._sync_running = False
        logger.info("[Trakt] Sync scheduler stopped")

    # ── Dashboard ─────────────────────────────────────────────────

    def get_dashboard_widget(self) -> Dict[str, Any]:
        shows = self.fetch_watchlist_shows()
        movies = self.fetch_watchlist_movies()
        count = len(shows) + len(movies)
        return {
            'enabled': True,
            'pill': {
                'icon': 'fas fa-star',
                'icon_color': 'text-warning',
                'template': f'{count}',
                'fields': ['watchlist_count']
            },
            'watchlist_count': count,
            'has_custom_widget': True,
            'has_dashboard_section': True
        }

    def get_watchlist_with_status(self) -> List[dict]:
        shows = self.fetch_watchlist_shows()
        movies = self.fetch_watchlist_movies()
        all_items = shows + movies
        sync_data = _load_sync_data()

        enriched = []
        for item in all_items:
            tmdb_id = item.get('tmdb_id')
            media_type = item.get('media_type', 'show')
            item_key = f"{media_type}_{tmdb_id}"
            synced = sync_data['synced_items'].get(item_key, {})

            if synced:
                status = synced.get('status', 'on_watchlist')
            elif media_type == 'show' and self._check_sonarr(tmdb_id):
                status = 'available'
            elif media_type == 'movie' and self._check_radarr(tmdb_id):
                status = 'available'
            else:
                status = 'on_watchlist'

            enriched.append({
                'title': item.get('title', 'Unknown'),
                'tmdb_id': tmdb_id,
                'media_type': media_type,
                'year': item.get('year', ''),
                'status': status,
                'synced_at': synced.get('synced_at'),
            })

        return enriched

    # ── Blueprint ─────────────────────────────────────────────────

    def create_blueprint(self) -> Blueprint:
        bp = Blueprint('trakt_integration', __name__, url_prefix='/api/integration/trakt')
        integration = self

        @bp.route('/watchlist', methods=['POST'])
        def trigger_sync():
            threading.Thread(
                target=integration.sync_watchlist,
                daemon=True, name='TraktManualSync'
            ).start()
            return jsonify({'status': 'success', 'message': 'Trakt sync started'}), 200

        @bp.route('/sync', methods=['POST'])
        def sync_now():
            threading.Thread(
                target=integration.sync_watchlist,
                daemon=True, name='TraktManualSync'
            ).start()
            return jsonify({'status': 'success', 'message': 'Trakt sync started'}), 200

        @bp.route('/status', methods=['GET'])
        def get_status():
            cfg = integration._get_trakt_config()
            sync_data = _load_sync_data()
            authenticated = bool((cfg or {}).get('access_token'))
            expires_at = (cfg or {}).get('expires_at')
            token_valid = False
            if expires_at and authenticated:
                try:
                    exp = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                    token_valid = exp > datetime.now(timezone.utc)
                except Exception:
                    pass
            return jsonify({
                'configured': bool((cfg or {}).get('client_id')),
                'authenticated': authenticated,
                'token_valid': token_valid,
                'expires_at': expires_at,
                'sync_enabled': (cfg or {}).get('sync_enabled', False),
                'sync_interval_minutes': (cfg or {}).get('sync_interval_minutes', 60),
                'sync_running': integration._sync_running,
                'last_full_sync': sync_data.get('last_full_sync'),
                'stats': sync_data.get('stats', {}),
            })

        @bp.route('/watchlist-html', methods=['GET'])
        def get_watchlist_html():
            cfg = integration._get_trakt_config()
            sync_enabled = (cfg or {}).get('sync_enabled', False)
            client_id = (cfg or {}).get('client_id', '')
            if not cfg or not client_id or not sync_enabled:
                return ('', 204)

            items = integration.get_watchlist_with_status()
            status_label = {
                'on_watchlist': '<span class="badge bg-secondary">On Watchlist</span>',
                'pending_selection': '<span class="badge bg-warning text-dark">Pending</span>',
                'added_to_sonarr': '<span class="badge bg-info">Added</span>',
                'added_to_radarr': '<span class="badge bg-info">Added</span>',
                'already_exists': '<span class="badge bg-success">Available</span>',
                'available': '<span class="badge bg-success">Available</span>',
                'watched': '<span class="badge bg-primary">Watched</span>',
                'error': '<span class="badge bg-danger">Error</span>',
            }

            rows = []
            for item in items:
                badge = status_label.get(
                    item['status'],
                    f'<span class="badge bg-secondary">{item["status"]}</span>'
                )
                type_icon = 'fas fa-tv' if item['media_type'] == 'show' else 'fas fa-film'
                rows.append(
                    f'<tr>'
                    f'<td><i class="{type_icon} me-2 text-muted"></i>{item["title"]}</td>'
                    f'<td>{item.get("year", "")}</td>'
                    f'<td>{badge}</td>'
                    f'</tr>'
                )

            if not rows:
                rows = ['<tr><td colspan="3" class="text-center text-muted py-3">'
                        'Trakt watchlist is empty or not authenticated</td></tr>']

            html = f'''
            <div id="trakt-watchlist-content">
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <small class="text-muted">{len(items)} item(s) on watchlist</small>
                    <button class="btn btn-sm btn-outline-secondary"
                            onclick="syncTraktWatchlist()">
                        <i class="fas fa-sync-alt me-1"></i>Sync Now
                    </button>
                </div>
                <div class="table-responsive">
                    <table class="table table-sm table-dark table-hover mb-0">
                        <thead>
                            <tr>
                                <th>Title</th>
                                <th>Year</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>{"".join(rows)}</tbody>
                    </table>
                </div>
            </div>
            '''
            return html

        return bp


integration = TraktIntegration()

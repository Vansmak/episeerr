"""
Arvio Integration for Episeerr

Provides:
  - Webhook receiver for Arvio playback events (progress-based episode processing)
  - Watchlist sync: poll Arvio's LAN REST API and add new items to Sonarr/Radarr
  - Dashboard section showing watchlist items with status labels
"""

import os
import json
import logging
import threading
import time
import subprocess
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
from flask import Blueprint, request, jsonify
from episeerr_utils import http
from integrations.base import ServiceIntegration

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────
#  Sync data (data/arvio_sync.json)
# ──────────────────────────────────────────────────────────────────

SYNC_DATA_FILE = os.path.join(os.getcwd(), 'data', 'arvio_sync.json')
SETTINGS_FILE = os.path.join(os.getcwd(), 'data', 'arvio_settings.json')


def _load_sync_data() -> dict:
    try:
        if os.path.exists(SYNC_DATA_FILE):
            with open(SYNC_DATA_FILE, 'r') as fh:
                return json.load(fh)
    except Exception as exc:
        logger.error(f"[Arvio] Error loading sync data: {exc}")
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
        logger.error(f"[Arvio] Error saving sync data: {exc}")


# ──────────────────────────────────────────────────────────────────
#  Integration class
# ──────────────────────────────────────────────────────────────────

class ArvioIntegration(ServiceIntegration):

    _sync_thread: Optional[threading.Thread] = None
    _sync_running: bool = False

    # ── Metadata ──────────────────────────────────────────────────

    @property
    def service_name(self) -> str:
        return 'arvio'

    @property
    def display_name(self) -> str:
        return 'Arvio'

    @property
    def description(self) -> str:
        return 'Arvio Android TV app — playback webhooks and watchlist sync over LAN'

    @property
    def icon(self) -> str:
        return 'https://cdn.jsdelivr.net/gh/selfhosters/unraid-CA-templates/templates/img/arflix.png'

    @property
    def category(self) -> str:
        return 'media'

    @property
    def default_port(self) -> int:
        return 7979

    # ── Connection / Stats ────────────────────────────────────────

    def test_connection(self, url: str, api_key: str) -> Tuple[bool, str]:
        # url is the first device URL; try all configured devices
        cfg = self._get_config()
        device_urls = (cfg or {}).get('device_urls') or ([url.rstrip('/')] if url else [])
        if not device_urls:
            return False, 'No device URLs configured'
        results = []
        for device_url in device_urls:
            try:
                resp = http.get(f"{device_url}/watchlist", timeout=5)
                if resp.ok:
                    results.append(f"{device_url.split('//')[-1]}: {len(resp.json())} item(s)")
                else:
                    results.append(f"{device_url.split('//')[-1]}: HTTP {resp.status_code}")
            except Exception as exc:
                results.append(f"{device_url.split('//')[-1]}: {exc}")
        connected = any('item(s)' in r for r in results)
        return connected, ' | '.join(results)

    def get_dashboard_stats(self, url: str, api_key: str) -> Dict[str, Any]:
        ok, msg = self.test_connection(url, api_key)
        return {'connected': ok, 'message': msg}

    # ── Setup fields ──────────────────────────────────────────────

    def get_setup_fields(self) -> Optional[List[Dict]]:
        # URL field handled via textarea in get_custom_setup_html so multiple devices are supported.
        return []

    def get_custom_setup_html(self, saved_values: dict = None) -> str:
        saved_values = saved_values or {}
        cfg = saved_values

        progress_threshold = cfg.get('progress_threshold', 80)
        sync_interval = cfg.get('sync_interval_minutes', 15)
        sync_enabled = cfg.get('sync_enabled', False)

        enabled_checked = 'checked' if sync_enabled else ''

        interval_options = ''.join(
            f'<option value="{v}" {"selected" if v == sync_interval else ""}>{lbl}</option>'
            for v, lbl in [
                (5, '5 minutes'), (10, '10 minutes'), (15, '15 minutes'),
                (30, '30 minutes'), (60, '1 hour'), (120, '2 hours')
            ]
        )

        # Pre-populate textarea with saved device URLs (one per line)
        existing_urls = cfg.get('device_urls') or []
        if not existing_urls and cfg.get('url'):
            existing_urls = [cfg['url']]
        device_urls_value = '\n'.join(existing_urls)

        # Use first URL for the webhook example
        first_url = existing_urls[0].rstrip('/') if existing_urls else '&lt;episeerr-host&gt;:5002'

        return f'''
        <!-- ── Arvio Devices ────────────────────────────────── -->
        <div class="mb-4">
            <h6 class="mb-2">
                <i class="fas fa-tv text-primary me-2"></i>Arvio Devices
            </h6>
            <label class="form-label">Device URLs <small class="text-muted">(one per line)</small></label>
            <textarea class="form-control form-control-sm font-monospace"
                      name="arvio-device-urls" rows="3"
                      placeholder="http://192.168.1.10:7979&#10;http://192.168.1.11:7979"
                      style="resize:vertical;">{device_urls_value}</textarea>
            <small class="text-muted">
                LAN address of each Android TV running Arvio with Watchlist API enabled (default port 7979).
                Watchlists from all devices are merged and deduplicated when syncing to Sonarr/Radarr.
            </small>
        </div>

        <!-- ── Playback Detection ───────────────────────────── -->
        <div style="border-top:1px solid rgba(255,255,255,0.1);margin-top:20px;padding-top:20px;">
            <h6 class="mb-3">
                <i class="fas fa-play-circle text-success me-2"></i>Playback Detection
            </h6>
            <small class="text-muted d-block mb-3">
                Configure Arvio: <strong>Settings → Integrations → Webhook URL</strong><br>
                URL: <code>http://{first_url}/api/integration/arvio/webhook</code>
            </small>
            <div class="row">
                <div class="col-md-4 mb-3">
                    <label class="form-label">Progress Threshold (%)</label>
                    <input type="number" class="form-control form-control-sm"
                           name="arvio-progress-threshold" value="{progress_threshold}"
                           min="1" max="99" style="max-width:100px;">
                    <small class="text-muted">Process episode when progress reaches this percentage</small>
                </div>
            </div>
        </div>

        <!-- ── Watchlist Auto-Sync ───────────────────────────── -->
        <div style="border-top:1px solid rgba(255,255,255,0.1);margin-top:20px;padding-top:20px;">
            <h6 class="mb-3">
                <i class="fas fa-sync-alt text-info me-2"></i>Watchlist Auto-Sync
            </h6>
            <div class="row">
                <div class="col-md-6 mb-3">
                    <div class="form-check form-switch">
                        <input type="checkbox" class="form-check-input" id="arvio-sync-enabled"
                               name="arvio-sync-enabled" {enabled_checked}>
                        <label class="form-check-label" for="arvio-sync-enabled">Enable automatic sync</label>
                    </div>
                    <small class="text-muted">
                        Periodically poll all Arvio devices and add new items to Sonarr/Radarr
                    </small>
                </div>
                <div class="col-md-6 mb-3">
                    <label class="form-label">Sync Interval</label>
                    <select class="form-select form-select-sm" name="arvio-sync-interval">
                        {interval_options}
                    </select>
                </div>
            </div>
        </div>
        '''

    def preprocess_save_data(self, normalized_data: dict) -> None:
        from settings_db import get_service
        existing_cfg = (get_service('arvio') or {}).get('config') or {}

        # Parse device URLs from textarea (one per line); prefix already stripped by save route
        raw_urls = normalized_data.pop('device-urls', '') or ''
        device_urls = [u.strip().rstrip('/') for u in raw_urls.splitlines() if u.strip()]
        normalized_data['device_urls'] = device_urls
        # Keep first URL as the service-level url for test_connection compat
        normalized_data['url'] = device_urls[0] if device_urls else ''

        normalized_data['progress_threshold'] = float(
            normalized_data.pop('progress-threshold',
                                existing_cfg.get('progress_threshold', 80)) or 80
        )
        normalized_data['sync_enabled'] = normalized_data.pop(
            'sync-enabled', existing_cfg.get('sync_enabled', False)
        )
        normalized_data['sync_interval_minutes'] = int(
            normalized_data.pop('sync-interval',
                                existing_cfg.get('sync_interval_minutes', 15)) or 15
        )

    def on_after_save(self, normalized_data: dict) -> None:
        if normalized_data.get('sync_enabled') and not self._sync_running:
            self.start_sync_scheduler()
        elif not normalized_data.get('sync_enabled') and self._sync_running:
            self.stop_sync_scheduler()

    # ── Config helper ─────────────────────────────────────────────

    def _get_config(self) -> Optional[dict]:
        try:
            from settings_db import get_service
            svc = get_service('arvio', 'default')
            if not svc:
                return None
            cfg = svc.get('config') or {}
            # Support both new multi-device list and legacy single URL
            device_urls = cfg.get('device_urls') or []
            if not device_urls and svc.get('url'):
                device_urls = [svc['url'].rstrip('/')]
            device_urls = [u.rstrip('/') for u in device_urls if u.strip()]
            return {
                'url': device_urls[0] if device_urls else '',
                'device_urls': device_urls,
                'progress_threshold': float(cfg.get('progress_threshold', 80)),
                'sync_interval_minutes': int(cfg.get('sync_interval_minutes', 15)),
                'sync_enabled': cfg.get('sync_enabled', False),
            }
        except Exception as exc:
            logger.error(f"[Arvio] Could not load config: {exc}")
        return None

    # ── Episode processing ────────────────────────────────────────

    def _process_episode(self, series_name: str, season: int, episode: int,
                         user: str, progress: float) -> bool:
        try:
            from media_processor import get_series_id
            from episeerr_utils import reconcile_series_drift
            from episeerr import load_config, save_config

            series_id = get_series_id(series_name)
            final_rule = None

            if series_id:
                config = load_config()
                final_rule, modified = reconcile_series_drift(series_id, config)
                if modified:
                    save_config(config)

            temp_dir = os.path.join(os.getcwd(), 'temp')
            os.makedirs(temp_dir, exist_ok=True)

            payload = {
                'server_title': series_name,
                'server_season_num': int(season),
                'server_ep_num': int(episode),
                'sonarr_series_id': series_id,
                'rule': final_rule,
                'source': 'arvio',
            }

            temp_path = os.path.join(temp_dir, 'data_from_server.json')
            with open(temp_path, 'w') as fh:
                json.dump(payload, fh)

            result = subprocess.run(
                ['python3', os.path.join(os.getcwd(), 'media_processor.py')],
                capture_output=True, text=True,
            )

            if result.returncode != 0:
                logger.error(f"[Arvio] media_processor failed (rc={result.returncode}): {result.stderr}")
                return False

            logger.info(f"[Arvio] Processed {series_name} S{season}E{episode} for {user} at {progress:.1f}%")
            return True

        except Exception as exc:
            logger.error(f"[Arvio] _process_episode error: {exc}", exc_info=True)
            return False

    # ── Sonarr / Radarr helpers ───────────────────────────────────

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
            logger.error(f"[Arvio] Sonarr check error: {exc}")
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
            logger.error(f"[Arvio] Radarr check error: {exc}")
        return None

    def _add_tv_to_sonarr(self, item: dict) -> dict:
        try:
            import sonarr_utils
            prefs = sonarr_utils.load_preferences()
            headers = {'X-Api-Key': prefs['SONARR_API_KEY'], 'Content-Type': 'application/json'}
            sonarr_url = prefs['SONARR_URL']
            tmdb_id = item.get('tmdb_id')

            if not tmdb_id:
                return {'success': False, 'status': 'missing_id',
                        'message': f"No TMDB ID for {item.get('title')}"}

            existing = self._check_sonarr(tmdb_id)
            if existing:
                return {'success': True, 'status': 'already_exists',
                        'series_id': existing.get('id'),
                        'message': f"{item.get('title')} already in Sonarr"}

            resp = http.get(f"{sonarr_url}/api/v3/series/lookup?term=tmdb:{tmdb_id}",
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
                logger.info(f"[Arvio] Added {item.get('title')} to Sonarr (id={sid})")
                return {'success': True, 'status': 'added', 'series_id': sid,
                        'message': f"Added {item.get('title')} — pending selection"}
            if add_resp.status_code == 400 and 'already been added' in add_resp.text.lower():
                return {'success': True, 'status': 'already_exists',
                        'message': f"{item.get('title')} already in Sonarr"}
            return {'success': False, 'status': 'add_failed',
                    'message': f"Sonarr add failed ({add_resp.status_code})"}

        except Exception as exc:
            logger.error(f"[Arvio] _add_tv_to_sonarr error: {exc}")
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

            existing = self._check_radarr(tmdb_id)
            if existing:
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
                logger.info(f"[Arvio] Added {item.get('title')} to Radarr (id={mid})")
                return {'success': True, 'status': 'added', 'movie_id': mid,
                        'message': f"Added {item.get('title')} to Radarr"}
            if add_resp.status_code == 400 and 'already been added' in add_resp.text.lower():
                return {'success': True, 'status': 'already_exists',
                        'message': f"{item.get('title')} already in Radarr"}
            return {'success': False, 'status': 'add_failed',
                    'message': f"Radarr add failed ({add_resp.status_code})"}

        except Exception as exc:
            logger.error(f"[Arvio] _add_movie_to_radarr error: {exc}")
            return {'success': False, 'status': 'error', 'message': str(exc)}

    # ── Watchlist fetch ───────────────────────────────────────────

    def _fetch_watchlist_from(self, url: str) -> List[dict]:
        try:
            resp = http.get(f"{url}/watchlist", timeout=10)
            if resp.ok:
                return resp.json()
        except Exception as exc:
            logger.error(f"[Arvio] Could not fetch watchlist from {url}: {exc}")
        return []

    def _fetch_watchlist(self, device_urls: List[str]) -> List[dict]:
        """Fetch and deduplicate watchlists from all configured devices."""
        seen: set = set()
        combined: List[dict] = []
        for url in device_urls:
            for item in self._fetch_watchlist_from(url):
                key = f"{item.get('media_type', 'show')}_{item.get('tmdb_id')}"
                if key not in seen:
                    seen.add(key)
                    combined.append(item)
        return combined

    # ── Watchlist sync ────────────────────────────────────────────

    def sync_watchlist(self) -> dict:
        cfg = self._get_config()
        device_urls = (cfg or {}).get('device_urls', [])
        if not cfg or not device_urls:
            return {'success': False, 'message': 'Arvio not configured'}

        items = self._fetch_watchlist(device_urls)
        if not items:
            return {'success': True, 'message': 'Watchlist empty or unreachable', 'processed': 0}

        sync_data = _load_sync_data()
        results = {
            'success': True, 'processed': 0, 'skipped': 0,
            'added_tv': 0, 'added_movies': 0, 'already_exists': 0,
            'errors': 0, 'items': []
        }

        for item in items:
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
                        'synced_at': datetime.now().isoformat(), 'source': 'arvio',
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
                    'synced_at': datetime.now().isoformat(), 'source': 'arvio',
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
                        'synced_at': datetime.now().isoformat(), 'source': 'arvio',
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
                    'synced_at': datetime.now().isoformat(), 'source': 'arvio',
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
            f"[Arvio] Sync complete: {results['processed']} processed, "
            f"{results['added_tv']} TV added, {results['added_movies']} movies added, "
            f"{results['already_exists']} already exist, {results['errors']} errors"
        )
        return results

    # ── Scheduler ─────────────────────────────────────────────────

    def start_sync_scheduler(self) -> None:
        if self._sync_running:
            return
        cfg = self._get_config() or {}
        interval = cfg.get('sync_interval_minutes', 15)

        def _loop():
            self._sync_running = True
            time.sleep(30)
            while self._sync_running:
                try:
                    logger.info("[Arvio] Running scheduled watchlist sync …")
                    self.sync_watchlist()
                except Exception as exc:
                    logger.error(f"[Arvio] Scheduled sync error: {exc}", exc_info=True)
                try:
                    fresh = self._get_config()
                    interval = (fresh or {}).get('sync_interval_minutes', 15)
                except Exception:
                    pass
                time.sleep(interval * 60)

        self._sync_thread = threading.Thread(target=_loop, daemon=True, name='arvio-sync')
        self._sync_thread.start()
        logger.info(f"[Arvio] Sync scheduler started (every {interval} min)")

    def stop_sync_scheduler(self) -> None:
        self._sync_running = False
        logger.info("[Arvio] Sync scheduler stopped")

    # ── Dashboard ─────────────────────────────────────────────────

    def get_dashboard_widget(self) -> Dict[str, Any]:
        cfg = self._get_config()
        device_urls = (cfg or {}).get('device_urls', [])
        count = 0
        if device_urls:
            items = self._fetch_watchlist(device_urls)
            count = len(items)
        return {
            'enabled': True,
            'pill': {
                'icon': 'fas fa-mobile-alt',
                'icon_color': 'text-info',
                'template': f'{count}',
                'fields': ['watchlist_count']
            },
            'watchlist_count': count,
            'has_custom_widget': True,
            'has_dashboard_section': True
        }

    def get_watchlist_with_status(self) -> List[dict]:
        cfg = self._get_config()
        device_urls = (cfg or {}).get('device_urls', [])
        if not cfg or not device_urls:
            return []

        items = self._fetch_watchlist(device_urls)
        sync_data = _load_sync_data()

        enriched = []
        for item in items:
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
                'poster_path': item.get('poster_path', ''),
            })

        return enriched

    # ── Blueprint ─────────────────────────────────────────────────

    def create_blueprint(self) -> Blueprint:
        bp = Blueprint('arvio_integration', __name__, url_prefix='/api/integration/arvio')
        integration = self

        @bp.route('/webhook', methods=['POST'])
        def arvio_webhook():
            try:
                data = request.get_json(force=True, silent=True) or {}
            except Exception:
                return jsonify({'status': 'error', 'message': 'Invalid JSON'}), 400

            event = data.get('event', '')
            media_type = data.get('media_type', '')
            title = data.get('title', 'Unknown')
            tmdb_id = data.get('tmdb_id')
            season = data.get('season')
            episode = data.get('episode')
            progress = int(data.get('progress_percent', 0))
            position = data.get('position_seconds', 0)
            duration = data.get('duration_seconds', 0)

            logger.info(
                f"[Arvio webhook] event={event!r} type={media_type!r} "
                f"title={title!r} S{season}E{episode} {progress}%"
            )

            if event == 'start' and media_type == 'tv' and all(
                    [title, season is not None, episode is not None]):
                try:
                    from media_processor import is_held_activation_episode, \
                        processed_jellyfin_episodes, get_episode_tracking_key
                    is_activation, _ = is_held_activation_episode(
                        title, int(season), int(episode)
                    )
                    if is_activation:
                        logger.info(f"[Arvio] Held activation: {title} S{season}E{episode}")
                        key = get_episode_tracking_key(title, int(season), int(episode), 'arvio')
                        processed_jellyfin_episodes.add(key)
                        threading.Thread(
                            target=integration._process_episode,
                            args=(title, int(season), int(episode), 'arvio', 0.0),
                            daemon=True, name='ArvioHeldActivation'
                        ).start()
                        return jsonify({'status': 'success', 'message': 'Held activation triggered'}), 200
                except Exception as exc:
                    logger.error(f"[Arvio] Held activation check failed: {exc}")

            elif event == 'progress' and media_type == 'tv' and all(
                    [title, season is not None, episode is not None]):
                try:
                    from media_processor import processed_jellyfin_episodes, get_episode_tracking_key
                    cfg = integration._get_config()
                    threshold = (cfg or {}).get('progress_threshold', 80)
                    if progress >= threshold:
                        key = get_episode_tracking_key(title, int(season), int(episode), 'arvio')
                        if key not in processed_jellyfin_episodes:
                            processed_jellyfin_episodes.add(key)
                            threading.Thread(
                                target=integration._process_episode,
                                args=(title, int(season), int(episode), 'arvio', float(progress)),
                                daemon=True, name='ArvioProgress'
                            ).start()
                            return jsonify({'status': 'success',
                                            'message': f'Processing at {progress}%'}), 200
                        else:
                            return jsonify({'status': 'success', 'message': 'Already processed'}), 200
                except Exception as exc:
                    logger.error(f"[Arvio] Progress handler error: {exc}")

            elif event in ('pause', 'stop'):
                logger.debug(f"[Arvio] {event} for {title} — no action")

            return jsonify({'status': 'success'}), 200

        @bp.route('/watchlist', methods=['GET'])
        def get_watchlist():
            cfg = integration._get_config()
            device_urls = (cfg or {}).get('device_urls', [])
            sync_enabled = (cfg or {}).get('sync_enabled', False)
            if not cfg or not device_urls or not sync_enabled:
                return ('', 204)

            items = integration.get_watchlist_with_status()

            status_colors = {
                'on_watchlist': '#6c757d',
                'pending_selection': '#ffc107',
                'added_to_sonarr': '#0dcaf0',
                'added_to_radarr': '#0dcaf0',
                'already_exists': '#198754',
                'available': '#198754',
                'watched': '#0d6efd',
                'error': '#dc3545',
            }
            status_labels = {
                'on_watchlist': 'Watchlist',
                'pending_selection': 'Pending',
                'added_to_sonarr': 'Added',
                'added_to_radarr': 'Added',
                'already_exists': 'Available',
                'available': 'Available',
                'watched': 'Watched',
                'error': 'Error',
            }
            status_icons = {
                'on_watchlist': 'fa-bookmark',
                'pending_selection': 'fa-user-clock',
                'added_to_sonarr': 'fa-check',
                'added_to_radarr': 'fa-check',
                'already_exists': 'fa-check-circle',
                'available': 'fa-check-circle',
                'watched': 'fa-eye',
                'error': 'fa-exclamation-triangle',
            }

            items_html = ''
            for item in items:
                title = item.get('title', 'Unknown')
                year = f" ({item.get('year')})" if item.get('year') else ''
                media_type = item.get('media_type', 'movie')
                type_icon = 'fa-tv' if media_type == 'show' else 'fa-film'
                status = item.get('status', 'on_watchlist')
                color = status_colors.get(status, '#6c757d')
                label = status_labels.get(status, status)
                icon = status_icons.get(status, 'fa-bookmark')
                poster = item.get('poster_path') or '/static/placeholder-poster.png'
                tmdb_id = item.get('tmdb_id', '')

                items_html += f'''
                <div class="watchlist-item" data-status="{status}" data-type="{media_type}" data-tmdb-id="{tmdb_id}">
                    <div class="watchlist-poster-wrap">
                        <img src="{poster}" class="watchlist-poster" alt="{title}">
                        <span class="watchlist-type-badge">
                            <i class="fas {type_icon}"></i>
                        </span>
                        <span class="watchlist-status-badge" style="background: {color};">
                            <i class="fas {icon}" style="font-size: 9px;"></i> {label}
                        </span>
                    </div>
                    <div class="watchlist-title">{title}{year}</div>
                </div>
                '''

            if not items_html:
                return ('', 204)

            sync_data = _load_sync_data()
            last_sync = sync_data.get('last_full_sync')
            if last_sync:
                try:
                    from datetime import datetime as dt
                    ago = (dt.now() - dt.fromisoformat(last_sync)).total_seconds()
                    sync_text = f"Synced {int(ago/60)}m ago" if ago < 3600 else f"Synced {int(ago/3600)}h ago"
                except Exception:
                    sync_text = "Synced"
            else:
                sync_text = "Sync pending..."

            html = f'''
            <div class="watchlist-container">
                <div class="watchlist-scroll">
                    {items_html}
                </div>
            </div>
            '''
            return html

        @bp.route('/status', methods=['GET'])
        def get_status():
            cfg = integration._get_config()
            sync_data = _load_sync_data()
            arvio_url = (cfg or {}).get('url', '')
            connected = False
            if arvio_url:
                ok, _ = integration.test_connection(arvio_url, '')
                connected = ok
            return jsonify({
                'configured': bool(arvio_url),
                'connected': connected,
                'sync_enabled': (cfg or {}).get('sync_enabled', False),
                'sync_interval_minutes': (cfg or {}).get('sync_interval_minutes', 15),
                'sync_running': integration._sync_running,
                'last_full_sync': sync_data.get('last_full_sync'),
                'stats': sync_data.get('stats', {}),
            })

        @bp.route('/sync', methods=['POST'])
        def trigger_sync():
            threading.Thread(
                target=integration.sync_watchlist,
                daemon=True, name='ArvioManualSync'
            ).start()
            return jsonify({'status': 'success', 'message': 'Sync started'}), 200

        @bp.route('/settings', methods=['GET'])
        def get_settings():
            if not os.path.exists(SETTINGS_FILE):
                return jsonify({'error': 'No settings stored'}), 404
            try:
                with open(SETTINGS_FILE, 'r') as fh:
                    return fh.read(), 200, {'Content-Type': 'application/json'}
            except Exception as exc:
                logger.error(f"[Arvio] Error reading settings: {exc}")
                return jsonify({'error': str(exc)}), 500

        @bp.route('/settings', methods=['PUT'])
        def put_settings():
            body = request.get_data()
            if not body:
                return jsonify({'error': 'Empty body'}), 400
            try:
                json.loads(body)  # validate JSON
            except Exception:
                return jsonify({'error': 'Invalid JSON'}), 400
            try:
                os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
                with open(SETTINGS_FILE, 'wb') as fh:
                    fh.write(body)
                logger.info("[Arvio] Settings saved")
                return jsonify({'status': 'ok'}), 200
            except Exception as exc:
                logger.error(f"[Arvio] Error saving settings: {exc}")
                return jsonify({'error': str(exc)}), 500

        return bp


integration = ArvioIntegration()

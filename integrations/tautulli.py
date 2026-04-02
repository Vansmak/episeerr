"""
Tautulli Integration for Episeerr
──────────────────────────────────
Provides:   Webhook receiver for Plex watch events, optional watch-history override
Auth:       API key via Tautulli Settings → Web Interface
Webhooks:   POST /api/integration/tautulli/webhook  (new)
            POST /webhook                            (legacy — still works)

Set up in Tautulli:
  Settings → Notification Agents → Webhook
  Trigger:  "Watched"
  URL:      http://<episeerr-host>:5002/api/integration/tautulli/webhook
"""

import os
import json
import requests
from episeerr_utils import http
import logging
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request
from integrations.base import ServiceIntegration

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
#  Core watch-event processor (shared with legacy /webhook route)
# ══════════════════════════════════════════════════════════════════

def process_watch_event(data: dict) -> dict:
    """
    Handle a Tautulli "watched" webhook payload.

    Extracts series/episode identifiers, performs Sonarr tag-sync and
    drift correction, then spawns media_processor.py to process the
    next-episode logic.

    Returns {'status': 'success'} or {'status': 'error', 'message': str}.
    Called by both the integration webhook route and the legacy /webhook
    backward-compatibility route in episeerr.py.
    """
    try:
        # {show_name} is TV-only in Tautulli; movies send it empty — fall back to {title}
        series_title   = (data.get('plex_title') or data.get('plex_movie_title') or
                          data.get('server_title') or 'Unknown')
        season_number  = data.get('plex_season_num') or data.get('server_season_num')
        episode_number = data.get('plex_ep_num')   or data.get('server_ep_num')
        thetvdb_id     = data.get('thetvdb_id')
        themoviedb_id  = data.get('themoviedb_id')

        # Movie detection: no season/episode numbers = movie watch event
        # Tautulli sends "0" for movies (not empty), so treat 0/"0" as absent
        def _absent(val):
            return not val or str(val).strip() in ('0', '')

        if _absent(season_number) and _absent(episode_number):
            logger.info(f"[Tautulli] Movie watched: '{series_title}' (tmdb={themoviedb_id or 'unknown'})")
            if themoviedb_id:
                try:
                    from integrations.plex import PlexIntegration
                    plex_integration = PlexIntegration()
                    plex_integration.mark_item_watched(str(themoviedb_id), 'movie')
                except Exception as e:
                    logger.warning(f"[Tautulli] Could not mark movie watched in watchlist: {e}")
            else:
                logger.warning(f"[Tautulli] Movie '{series_title}' has no TMDB ID — watchlist not updated. Add {{themoviedb_id}} to your Tautulli template.")
            return {'status': 'success'}

        from media_processor import get_series_id
        series_id = get_series_id(series_title, thetvdb_id, themoviedb_id)
        final_rule = None

        if not series_id:
            logger.warning(f"[Tautulli] Cannot find Sonarr ID for '{series_title}'")
        else:
            from episeerr import load_config, save_config
            from episeerr_utils import reconcile_series_drift
            config = load_config()
            final_rule, modified = reconcile_series_drift(series_id, config)
            if modified:
                save_config(config)

        # Write temp file + spawn media_processor
        temp_dir = os.path.join(os.getcwd(), 'temp')
        os.makedirs(temp_dir, exist_ok=True)

        payload = {
            "server_title":    series_title,
            "server_season_num": season_number,
            "server_ep_num":   episode_number,
            "thetvdb_id":      thetvdb_id,
            "themoviedb_id":   themoviedb_id,
            "sonarr_series_id": series_id,
            "rule":            final_rule,
        }

        temp_path = os.path.join(temp_dir, 'data_from_server.json')
        with open(temp_path, 'w') as fh:
            json.dump(payload, fh)

        result = subprocess.run(
            ["python3", os.path.join(os.getcwd(), "media_processor.py")],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            logger.error(
                f"[Tautulli] media_processor failed (rc={result.returncode}): {result.stderr}"
            )
        else:
            logger.info(f"[Tautulli] Processed {series_title} S{season_number}E{episode_number}")

        # Update Plex Watchlist watched status for TV
        if themoviedb_id:
            try:
                from integrations.plex import PlexIntegration
                PlexIntegration().mark_item_watched(str(themoviedb_id), 'tv')
            except Exception as e:
                logger.debug(f"[Tautulli] Could not update watchlist watched status: {e}")

        return {'status': 'success'}

    except Exception as exc:
        logger.error(f"[Tautulli] process_watch_event error: {exc}", exc_info=True)
        return {'status': 'error', 'message': str(exc)}


def get_tautulli_watch_history(rating_key: str) -> Optional[Dict]:
    """
    Query Tautulli for the most recent watch timestamp for a given Plex rating_key.
    Returns {'last_watched': <unix timestamp>} or None.
    """
    try:
        from settings_db import get_service
        svc = get_service('tautulli', 'default')
        if not svc:
            return None
        url     = svc.get('url', '').rstrip('/')
        api_key = svc.get('api_key', '')
        if not url or not api_key:
            return None

        resp = http.get(
            f"{url}/api/v2",
            params={'apikey': api_key, 'cmd': 'get_item_user_stats', 'rating_key': rating_key},
            timeout=10,
        )
        if resp.ok:
            entries = resp.json().get('response', {}).get('data', [])
            if entries:
                last_watched = max((e.get('last_watched', 0) for e in entries), default=0)
                return {'last_watched': last_watched}
    except Exception as exc:
        logger.warning(f"[Tautulli] get_tautulli_watch_history error: {exc}")
    return None


# ══════════════════════════════════════════════════════════════════
#  Integration class
# ══════════════════════════════════════════════════════════════════

class TautulliIntegration(ServiceIntegration):

    # ── Metadata ──────────────────────────────────────────────────

    @property
    def service_name(self) -> str:
        return 'tautulli'

    @property
    def display_name(self) -> str:
        return 'Tautulli'

    @property
    def description(self) -> str:
        return 'Plex analytics — episode-watch webhooks and optional watch-history override'

    @property
    def icon(self) -> str:
        return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/tautulli.png'

    @property
    def category(self) -> str:
        return 'media'

    @property
    def default_port(self) -> int:
        return 8181

    # ── Setup fields ──────────────────────────────────────────────

    def get_setup_fields(self) -> Optional[List[Dict]]:
        return [
            {
                'name':        'url',
                'label':       'Tautulli URL',
                'type':        'text',
                'placeholder': 'http://192.168.1.100:8181',
                'required':    True,
                'help_text':   'Base URL of your Tautulli instance',
            },
            {
                'name':        'api_key',
                'label':       'API Key',
                'type':        'text',
                'placeholder': 'Your Tautulli API key',
                'required':    True,
                'help_text':   'Settings → Web Interface → API Key',
            },
        ]

    def get_custom_setup_html(self, saved_values: dict = None) -> str:
        saved_values  = saved_values or {}
        override      = saved_values.get('override_plex', False)
        override_chk  = 'checked' if override else ''

        return f'''
        <div style="border-top:1px solid rgba(255,255,255,0.1);margin-top:16px;padding-top:16px;">
            <h6 class="mb-3">
                <i class="fas fa-exchange-alt text-warning me-2"></i>Watch History Source
            </h6>
            <div class="form-check form-switch mb-2">
                <input type="checkbox" class="form-check-input" id="tautulli-override_plex"
                       name="tautulli-override_plex" {override_chk}>
                <label class="form-check-label" for="tautulli-override_plex">
                    Use Tautulli for watch history instead of Plex
                </label>
            </div>
            <small class="text-muted d-block mb-4">
                When enabled, Tautulli is queried for all watch-date lookups (cleanup, dormant
                detection, grace periods) instead of the Plex API.  Useful if you prefer
                Tautulli&rsquo;s richer history while still using Plex direct webhooks.
            </small>

            <hr style="border-color:rgba(255,255,255,0.08);">
            <h6 class="mb-2" style="font-size:13px;">
                <i class="fas fa-info-circle text-info me-2"></i>Webhook Setup
            </h6>
            <p class="text-muted mb-0" style="font-size:12px;">
                In Tautulli: <strong>Settings → Notification Agents → Webhook</strong><br>
                Trigger: <em>Watched</em> &nbsp;·&nbsp; URL: <code>/api/integration/tautulli/webhook</code><br>
                <strong class="text-success">Leave Conditions blank</strong> — no media type filter needed. Episeerr detects TV vs movie automatically.<br>
                <span class="text-warning">Note:</span> If using Plex native webhook for watch detection, do <strong>not</strong> also enable this Tautulli watched webhook — choose one source only.
            </p>
            <pre class="mt-2 mb-0 p-2 rounded" style="background:#1a1a2e;font-size:11px;">{{\n  "plex_title": "{{show_name}}",\n  "plex_movie_title": "{{title}}",\n  "plex_season_num": "{{season_num}}",\n  "plex_ep_num": "{{episode_num}}",\n  "thetvdb_id": "{{thetvdb_id}}",\n  "themoviedb_id": "{{themoviedb_id}}"\n}}</pre>
        </div>
        '''

    def preprocess_save_data(self, normalized_data: dict) -> None:
        """Normalise override_plex to a proper bool."""
        val = normalized_data.get('override_plex', False)
        if isinstance(val, str):
            normalized_data['override_plex'] = val.lower() in ('true', '1', 'on', 'yes')
        else:
            normalized_data['override_plex'] = bool(val)

    # ── Connection test ───────────────────────────────────────────

    def test_connection(self, url: str, api_key: str, **kwargs) -> Tuple[bool, str]:
        try:
            resp = http.get(
                f"{url.rstrip('/')}/api/v2",
                params={'apikey': api_key, 'cmd': 'get_server_info'},
                timeout=10,
            )
            if resp.ok:
                info   = resp.json().get('response', {}).get('data', {})
                server = info.get('pms_name', 'Plex')
                return True, f"Connected to Tautulli (Plex: {server})"
            if resp.status_code == 401:
                return False, "Invalid API key"
            return False, f"HTTP {resp.status_code}"
        except requests.exceptions.ConnectionError:
            return False, "Cannot connect — verify URL and that Tautulli is running"
        except requests.exceptions.Timeout:
            return False, "Connection timed out"
        except Exception as exc:
            return False, str(exc)

    # ── Dashboard ─────────────────────────────────────────────────

    def get_dashboard_stats(self, url: str = None, api_key: str = None) -> Dict[str, Any]:
        return {'configured': True}

    def get_dashboard_widget(self) -> Optional[Dict]:
        return None  # Tautulli has no dashboard widget

    # ── Flask blueprint ───────────────────────────────────────────

    def create_blueprint(self) -> Blueprint:
        bp = Blueprint(
            'tautulli_integration', __name__,
            url_prefix='/api/integration/tautulli',
        )

        @bp.route('/webhook', methods=['POST'])
        def tautulli_webhook():
            """
            Receive Tautulli 'Watched' events and trigger episode processing.

            Configure in Tautulli:
              Settings → Notification Agents → Webhook
              Trigger: Watched
              URL: http://<episeerr-host>:5002/api/integration/tautulli/webhook
            """
            logger.info("[Tautulli] Webhook received")
            data = request.get_json(silent=True) or {}
            if not data:
                return jsonify({'status': 'error', 'message': 'No data received'}), 400

            result      = process_watch_event(data)
            status_code = 200 if result['status'] == 'success' else 500
            return jsonify(result), status_code

        return bp


# Auto-discovery
integration = TautulliIntegration()

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
        series_title   = data.get('plex_title')   or data.get('server_title')   or 'Unknown'
        season_number  = data.get('plex_season_num') or data.get('server_season_num')
        episode_number = data.get('plex_ep_num')   or data.get('server_ep_num')
        thetvdb_id     = data.get('thetvdb_id')
        themoviedb_id  = data.get('themoviedb_id')

        from media_processor import get_series_id
        series_id = get_series_id(series_title, thetvdb_id, themoviedb_id)
        final_rule = None

        if not series_id:
            logger.warning(f"[Tautulli] Cannot find Sonarr ID for '{series_title}'")
        else:
            from episeerr import load_config, save_config
            import episeerr_utils
            from media_processor import move_series_in_config
            from episeerr_utils import validate_series_tag, sync_rule_tag_to_sonarr

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
                        logger.warning(
                            f"[Tautulli] DRIFT: config={config_rule} → tag={actual_tag_rule}"
                        )
                        move_series_in_config(series_id, config_rule, actual_tag_rule)
                        final_rule = actual_tag_rule
                    else:
                        logger.warning(
                            f"[Tautulli] No tag on series {series_id} "
                            f"→ restoring episeerr_{config_rule}"
                        )
                        sync_rule_tag_to_sonarr(series_id, config_rule)
                        final_rule = config_rule
                else:
                    final_rule = config_rule
                    logger.debug(f"[Tautulli] Tag matches config rule: {final_rule}")
            else:
                # Orphaned-tag detection: series not in config but may have an episeerr tag
                series = episeerr_utils.get_series_from_sonarr(series_id)
                if series:
                    tag_mapping = episeerr_utils.get_tag_mapping()
                    found = False
                    for tag_id in series.get('tags', []):
                        tag_name = tag_mapping.get(tag_id, '').lower()
                        if not tag_name.startswith('episeerr_'):
                            continue
                        rule_name = tag_name.replace('episeerr_', '')
                        if rule_name in ('default', 'select'):
                            continue
                        actual_rule = next(
                            (rn for rn in config['rules'] if rn.lower() == rule_name), None
                        )
                        if actual_rule:
                            import time
                            config['rules'][actual_rule].setdefault('series', {})[series_id_str] = {
                                'activity_date': int(time.time())
                            }
                            save_config(config)
                            logger.info(
                                f"[Tautulli] ORPHANED: Added series {series_id} "
                                f"('{series_title}') to '{actual_rule}' via Sonarr tag"
                            )
                            final_rule = actual_rule
                        else:
                            logger.warning(
                                f"[Tautulli] Found tag '{tag_name}' on series {series_id} "
                                f"but rule '{rule_name}' not in config"
                            )
                        found = True
                        break
                    if not found:
                        logger.info(
                            f"[Tautulli] Series {series_id} not in any rule — skipping tag sync"
                        )
                else:
                    logger.warning(f"[Tautulli] Cannot fetch series {series_id} from Sonarr")

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

        resp = requests.get(
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
                Trigger: <em>Watched</em><br>
                New URL:&nbsp;&nbsp;<code>/api/integration/tautulli/webhook</code><br>
                Legacy URL: <code>/webhook</code> (both are accepted)
            </p>
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
            resp = requests.get(
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

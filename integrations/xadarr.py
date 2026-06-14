"""
Xadarr Integration for Episeerr
───────────────────────────────
Provides:   Settings sync, playback webhooks,
            and a web dashboard at /xadarr for Xadarr Android TV app.

Sync API (prefix: /api/integration/xadarr):
    GET  /status           ← Xadarr pings this to verify the server is reachable
    GET  /settings         ← Xadarr pulls its full settings blob on new-device setup
    PUT  /settings         ← Xadarr pushes its settings blob (profile sync, addons, etc.)
    GET  /settings/backup  ← Download settings blob as a JSON file
    POST /settings/backup  ← Restore settings blob from an uploaded JSON file
    POST /webhook          ← Xadarr posts playback events (start/pause/stop/progress)
    GET  /pending          ← Returns series currently in episeerr_select state
    GET  /watchlist        ← Return watchlist array from current settings blob
    POST /watchlist        ← Add an item to the watchlist in the settings blob
    DELETE /watchlist/<type>/<id>  ← Remove a watchlist item

Addon manifest (root-level, for Xadarr addon manager):
    GET  /api/addon/xadarr-bridge/manifest.json

Dashboard API (also under /api/integration/xadarr):
    GET/DELETE /history              ← playback event log
    GET        /dashboard/player/state
    GET        /dashboard/player/events  ← SSE stream

Dashboard UI (prefix: /xadarr):
    GET /         ← SPA index.html
    GET /static/* ← JS / CSS

Data files:
    data/xadarr_settings.json        ← settings blob synced from TV app
    data/xadarr_history.json         ← playback event log (last 500)

Outbound webhooks (fires to xadarr-server):
    episode.grabbed   — Sonarr grabbed an episode for download
    episode.ready     — Sonarr imported a downloaded episode
    rule.triggered    — Episeerr ran a rule (media_processor)
    rule.assigned     — User assigned a rule to a pending series
    watchlist.requested — Series added to pending/selection queue
"""

import os
import json
import queue
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, jsonify, request, send_from_directory, Response, stream_with_context
from integrations.base import ServiceIntegration
from episeerr_utils import http as _http

logger = logging.getLogger(__name__)

# ── File paths ────────────────────────────────────────────────────────────────

_DATA_DIR      = os.path.join(os.getcwd(), "data")
_SETTINGS_FILE = os.path.join(_DATA_DIR, "xadarr_settings.json")
_HISTORY_FILE  = os.path.join(_DATA_DIR, "xadarr_history.json")

# ── Rule-processing dedup ─────────────────────────────────────────────────────
# Prevents triggering Sonarr rule processing more than once per watch session.
# Key: "{tmdb_id}:{season}:{episode}" or "{tmdb_id}:movie"
# Value: unix timestamp of when processing was triggered.
_processed_episodes: Dict[str, float] = {}
_processed_lock = threading.Lock()

_COMPLETION_THRESHOLD_DEFAULT = 85  # fallback if service config is missing
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "xadarr_static")
_LOCK = threading.Lock()

# ── Outbound xadarr-server webhooks ───────────────────────────────────────────

def _get_xadarr_server_url() -> Optional[str]:
    """Return the configured xadarr-server base URL, or None if not set."""
    try:
        from settings_db import get_service
        svc = get_service('xadarr', 'default')
        if svc and svc.get('url'):
            return svc['url'].rstrip('/')
    except Exception as exc:
        logger.debug(f"[Xadarr] Could not read server URL: {exc}")
    return None


def fire_xadarr_webhook(event: str, payload: dict) -> None:
    """POST an event to xadarr-server's inbound webhook in a background thread."""
    server_url = _get_xadarr_server_url()
    if not server_url:
        return

    def _post():
        try:
            body = {"event": event, **payload}
            _http.post(
                f"{server_url}/api/integration/xadarr/webhook",
                json=body,
                timeout=5,
            )
            logger.debug(f"[Xadarr] Fired webhook {event} to {server_url}")
        except Exception as exc:
            logger.debug(f"[Xadarr] Webhook {event} failed: {exc}")

    threading.Thread(target=_post, daemon=True).start()

# ── SSE player-state broadcast ────────────────────────────────────────────────

_sse_queues: list = []
_sse_lock = threading.Lock()
_player_state: dict = {
    "isPlaying": False, "isPaused": False,
    "title": "", "episodeTitle": "",
    "overview": "", "positionMs": 0, "durationMs": 0,
    "streamUrl": "", "isLive": False,
}


def _broadcast_player_state() -> None:
    payload = "data: " + json.dumps(_player_state) + "\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)


# ── JSON file helpers ─────────────────────────────────────────────────────────

def _load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_settings() -> Optional[dict]:
    with _LOCK:
        try:
            if not os.path.exists(_SETTINGS_FILE):
                return None
            with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.error(f"[Xadarr] Failed to load settings: {exc}")
            return None


def _save_settings(data: dict) -> bool:
    with _LOCK:
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            return True
        except Exception as exc:
            logger.error(f"[Xadarr] Failed to save settings: {exc}")
            return False


def _get_completion_threshold() -> float:
    try:
        from settings_db import get_service
        svc = get_service('xadarr', 'default')
        if svc and svc.get('config'):
            return float(svc['config'].get('progress_threshold', _COMPLETION_THRESHOLD_DEFAULT))
    except Exception:
        pass
    return _COMPLETION_THRESHOLD_DEFAULT


def _trigger_rule_processing(title: str, tmdb_id: str, season, episode) -> None:
    """
    Identify the Sonarr series and spawn media_processor.py to apply next-episode rules.
    Mirrors the flow in integrations/tautulli.py::process_watch_event().
    """
    try:
        from media_processor import get_series_id
        series_id = get_series_id(title, None, tmdb_id)
        if not series_id:
            logger.warning(f"[Xadarr] Could not find Sonarr series ID for '{title}' (tmdb={tmdb_id})")
            return

        logger.debug(f"[Xadarr] Found Sonarr series_id={series_id} for '{title}'")

        final_rule = None
        try:
            from episeerr import load_config, save_config
            from episeerr_utils import reconcile_series_drift
            config = load_config()
            final_rule, modified = reconcile_series_drift(series_id, config)
            if modified:
                save_config(config)
            logger.debug(f"[Xadarr] Rule for series {series_id}: {final_rule}")
        except Exception as drift_err:
            logger.warning(f"[Xadarr] Drift reconciliation skipped: {drift_err}")

        temp_dir = os.path.join(os.getcwd(), "temp")
        os.makedirs(temp_dir, exist_ok=True)
        payload = {
            "server_title":      title,
            "server_season_num": season,
            "server_ep_num":     episode,
            "themoviedb_id":     tmdb_id,
            "sonarr_series_id":  series_id,
            "rule":              final_rule,
        }
        temp_path = os.path.join(temp_dir, "data_from_server.json")
        with open(temp_path, "w") as fh:
            json.dump(payload, fh)

        result = subprocess.run(
            ["python3", os.path.join(os.getcwd(), "media_processor.py")],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.error(f"[Xadarr] media_processor failed (rc={result.returncode}): {result.stderr}")
        else:
            logger.info(f"[Xadarr] Rule processing complete for '{title}' S{season}E{episode}")
            if result.stdout:
                logger.debug(f"[Xadarr] media_processor stdout: {result.stdout[:500]}")

    except Exception as exc:
        logger.error(f"[Xadarr] _trigger_rule_processing error: {exc}", exc_info=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Integration class
# ══════════════════════════════════════════════════════════════════════════════

class XadarrIntegration(ServiceIntegration):

    # ── Metadata ──────────────────────────────────────────────────────────────

    @property
    def service_name(self) -> str:
        return "xadarr"

    @property
    def display_name(self) -> str:
        return "Xadarr"

    @property
    def description(self) -> str:
        return "Android TV media hub — settings sync and playback events"

    @property
    def icon(self) -> str:
        return ""

    @property
    def category(self) -> str:
        return "utility"

    @property
    def default_port(self) -> int:
        return 7979  # Xadarr's LAN watchlist server port

    def get_setup_fields(self):
        return [
            {
                "name":        "url",
                "label":       "Xadarr Sync-Server URL",
                "type":        "url",
                "placeholder": "http://192.168.x.x:7979",
                "required":    False,
                "help_text":   (
                    "LAN URL of your xadarr-server (port 7979). "
                    "Used for the dashboard quick-link."
                ),
            },
            {
                "name":        "progress_threshold",
                "label":       "Progress Threshold (%)",
                "type":        "text",
                "placeholder": "50",
                "required":    False,
                "help_text":   (
                    "Minimum watch % before Episeerr triggers next-episode rule processing. "
                    "Should match or be lower than the completion % configured in the Xadarr app / sync-server settings."
                ),
            },
        ]

    # ── Connection test (not used by Xadarr; satisfies base class) ─────────────

    def test_connection(self, url: str, api_key: str) -> Tuple[bool, str]:
        return True, "Xadarr connects to Episeerr, not the other way around."

    def get_dashboard_stats(self, url: str, api_key: str) -> Dict[str, Any]:
        settings = _load_settings()
        if not settings:
            return {"profiles": 0, "last_sync": None}
        profiles = settings.get("profiles") or []
        updated_at = settings.get("updatedAt")
        last_sync = None
        if updated_at:
            try:
                last_sync = datetime.fromtimestamp(updated_at / 1000, tz=timezone.utc).isoformat()
            except Exception:
                pass
        return {"profiles": len(profiles), "last_sync": last_sync}

    def get_dashboard_widget(self) -> Dict[str, Any]:
        settings = _load_settings()
        profiles = len((settings or {}).get("profiles") or [])
        return {
            "title":       self.display_name,
            "description": self.description,
            "icon":        self.icon,
            "stats":       [{"label": "Profiles", "value": profiles}],
            "status":      "connected" if settings else "not_configured",
        }

    # ── Flask blueprint ───────────────────────────────────────────────────────

    def create_blueprint(self) -> Blueprint:
        bp = Blueprint(
            "xadarr_integration", __name__,
            url_prefix="/api/integration/xadarr",
        )

        # ── GET /status ───────────────────────────────────────────────────────
        @bp.route("/status", methods=["GET"])
        def status():
            """
            Health check — Xadarr calls this to verify the server is reachable
            before saving the sync server URL or adding the Episeerr addon.
            """
            settings = _load_settings()
            profiles = len((settings or {}).get("profiles") or []) if settings else 0
            return jsonify({
                "status": "ok",
                "service": "episeerr",
                "xadarr_profiles": profiles,
                "settings_present": settings is not None,
            }), 200

        # ── GET /pending ──────────────────────────────────────────────────────────
        @bp.route("/pending", methods=["GET"])
        def get_pending():
            """
            Return series currently awaiting rule selection (episeerr_select state).
            Used by xadarr-server to show pending badges and by the TV app rule picker.
            """
            try:
                from settings_db import get_all_pending_requests
                rows = get_all_pending_requests()
            except Exception as exc:
                logger.error(f"[Xadarr] /pending DB error: {exc}")
                return jsonify([]), 200

            items = []
            for row in rows:
                tmdb_id = row.get("tmdb_id")
                poster = None
                if tmdb_id:
                    try:
                        from settings_db import get_service
                        tmdb_svc = get_service("tmdb", "default")
                        if tmdb_svc and tmdb_svc.get("api_key"):
                            r = _http.get(
                                f"https://api.themoviedb.org/3/tv/{tmdb_id}",
                                params={"api_key": tmdb_svc["api_key"]},
                                timeout=5,
                            )
                            if r.ok and r.json().get("poster_path"):
                                poster = "https://image.tmdb.org/t/p/w342" + r.json()["poster_path"]
                    except Exception:
                        pass

                items.append({
                    "id":       row.get("id"),
                    "seriesId": row.get("series_id"),
                    "title":    row.get("title", ""),
                    "tmdbId":   tmdb_id,
                    "tvdbId":   row.get("tvdb_id"),
                    "poster":   poster,
                    "createdAt": row.get("created_at"),
                })

            return jsonify(items), 200

        # ── GET /settings ─────────────────────────────────────────────────────
        @bp.route("/settings", methods=["GET"])
        def get_settings():
            """
            Return the full Xadarr settings blob.
            Xadarr calls this on new-device setup to restore all settings.
            Returns 404 if no settings have been saved yet.
            """
            data = _load_settings()
            if data is None:
                return jsonify({"error": "No settings saved yet"}), 404
            return jsonify(data), 200

        # ── PUT /settings ─────────────────────────────────────────────────────
        @bp.route("/settings", methods=["PUT"])
        def put_settings():
            """
            Save the full Xadarr settings blob.
            Xadarr calls this after any settings change or profile operation.
            """
            body = request.get_json(silent=True, force=True)
            if not body or not isinstance(body, dict):
                return jsonify({"error": "Expected a JSON object"}), 400
            ok = _save_settings(body)
            if not ok:
                return jsonify({"error": "Failed to write settings"}), 500
            profiles = len(body.get("profiles") or [])
            logger.info(f"[Xadarr] Settings saved — {profiles} profile(s)")
            return jsonify({"status": "saved", "profiles": profiles}), 200

        # ── POST /webhook ─────────────────────────────────────────────────────
        @bp.route("/webhook", methods=["POST"])
        def webhook():
            data = request.get_json(silent=True) or {}
            event      = data.get("event", "unknown")
            title      = data.get("title", "Unknown")
            tmdb_id    = data.get("tmdb_id")
            media_type = data.get("media_type", "?")
            progress   = float(data.get("progress_percent") or 0)
            season     = data.get("season")
            episode    = data.get("episode")

            ep_info = f" S{season:02d}E{episode:02d}" if season and episode else ""
            logger.info(
                f"[Xadarr] {event.upper()} — {title}{ep_info} "
                f"({media_type} tmdb={tmdb_id}) {progress}%"
            )

            # Log to history file
            entry = {
                "timestamp":    datetime.now(timezone.utc).isoformat(),
                "event":        event,
                "title":        title,
                "episodeTitle": data.get("episodeTitle", ep_info.strip()),
                "mediaType":    media_type,
                "tmdbId":       tmdb_id,
                "positionMs":   data.get("positionMs") or int((data.get("position_seconds") or 0) * 1000),
                "durationMs":   data.get("durationMs") or int((data.get("duration_seconds") or 0) * 1000),
                "streamUrl":    data.get("streamUrl", ""),
            }
            with _LOCK:
                history = _load_json(_HISTORY_FILE, [])
                history.insert(0, entry)
                _save_json(_HISTORY_FILE, history[:500])

            # Update live player state + broadcast to SSE clients
            global _player_state
            if event in ("start", "progress"):
                _player_state = {
                    "isPlaying":    True,
                    "isPaused":     False,
                    "title":        title,
                    "episodeTitle": entry["episodeTitle"],
                    "overview":     data.get("overview", ""),
                    "positionMs":   entry["positionMs"],
                    "durationMs":   entry["durationMs"],
                    "streamUrl":    entry["streamUrl"],
                    "isLive":       data.get("isLive", False),
                }
            elif event == "pause":
                _player_state = {**_player_state, "isPlaying": False, "isPaused": True}
            elif event in ("stop", "finish"):
                _player_state = {**_player_state, "isPlaying": False, "isPaused": False}
            _broadcast_player_state()

            # ── Rule processing ───────────────────────────────────────────────
            # Only TV episodes with a known TMDB ID trigger Sonarr rule processing.
            # Movies and live IPTV streams are skipped.
            is_tv = media_type in ("tv", "show", "series", "episode") and bool(season) and bool(episode)
            if is_tv and tmdb_id and not data.get("isLive"):
                ep_key = f"{tmdb_id}:{season}:{episode}"

                if event == "start":
                    # New watch session — reset dedup so this episode can be processed again
                    with _processed_lock:
                        _processed_episodes.pop(ep_key, None)
                    logger.debug(f"[Xadarr] Reset dedup for {ep_key}")

                elif event in ("progress", "stop", "finish"):
                    with _processed_lock:
                        already_processed = ep_key in _processed_episodes

                    if already_processed:
                        logger.debug(f"[Xadarr] {ep_key} already processed this session — skipping")
                    else:
                        threshold = _get_completion_threshold()
                        logger.debug(
                            f"[Xadarr] {event.upper()} {ep_key} at {progress}% "
                            f"(threshold={threshold}%)"
                        )
                        if progress >= threshold:
                            logger.info(
                                f"[Xadarr] Threshold reached for {title}{ep_info} "
                                f"({progress}% >= {threshold}%) — triggering rule processing"
                            )
                            _trigger_rule_processing(
                                title=title,
                                tmdb_id=str(tmdb_id),
                                season=season,
                                episode=episode,
                            )
                            with _processed_lock:
                                _processed_episodes[ep_key] = time.time()
                        else:
                            logger.debug(
                                f"[Xadarr] Below threshold ({progress}% < {threshold}%) — not processing"
                            )

            return jsonify({"status": "received"}), 200

        # ── GET /settings/backup ─────────────────────────────────────────────
        @bp.route("/settings/backup", methods=["GET"])
        def download_settings_backup():
            """
            Download the current settings blob as a JSON file.
            Mirrors xadarr-server's GET /api/integration/xadarr/settings/backup.
            """
            from flask import make_response
            data = _load_settings()
            if data is None:
                return jsonify({"error": "No settings saved yet"}), 404
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            resp = make_response(payload, 200)
            resp.headers["Content-Type"] = "application/json"
            resp.headers["Content-Disposition"] = "attachment; filename=xadarr_settings_backup.json"
            return resp

        # ── POST /settings/backup ─────────────────────────────────────────────
        @bp.route("/settings/backup", methods=["POST"])
        def upload_settings_backup():
            """
            Restore settings from a JSON file upload or raw JSON body.
            Mirrors xadarr-server's POST /api/integration/xadarr/settings/backup.
            """
            # Accept either multipart file upload or raw JSON body
            data = None
            if request.files.get("file"):
                try:
                    data = json.load(request.files["file"])
                except Exception as exc:
                    return jsonify({"error": f"Invalid JSON file: {exc}"}), 400
            else:
                data = request.get_json(silent=True, force=True)

            if not data or not isinstance(data, dict):
                return jsonify({"error": "Expected a JSON object"}), 400
            ok = _save_settings(data)
            if not ok:
                return jsonify({"error": "Failed to write settings"}), 500
            profiles = len(data.get("profiles") or [])
            logger.info(f"[Xadarr] Settings restored from backup — {profiles} profile(s)")
            return jsonify({"status": "restored", "profiles": profiles}), 200

        # ── GET/POST/DELETE /watchlist ────────────────────────────────────────
        @bp.route("/watchlist", methods=["GET"])
        def get_watchlist():
            """Return the watchlist array from the current settings blob."""
            settings = _load_settings() or {}
            return jsonify(settings.get("watchlist") or []), 200

        @bp.route("/watchlist", methods=["POST"])
        def add_watchlist_item():
            """Add an item to the watchlist in the settings blob."""
            item = request.get_json(silent=True, force=True)
            if not item or not isinstance(item, dict):
                return jsonify({"error": "Expected a JSON object"}), 400
            with _LOCK:
                settings = _load_settings() or {}
                watchlist = settings.get("watchlist") or []
                watchlist.append(item)
                settings["watchlist"] = watchlist
                _save_settings(settings)
            return jsonify({"status": "added", "count": len(watchlist)}), 200

        @bp.route("/watchlist/<media_type>/<int:media_id>", methods=["DELETE"])
        def remove_watchlist_item(media_type: str, media_id: int):
            """Remove a watchlist item by type and id."""
            with _LOCK:
                settings = _load_settings() or {}
                watchlist = settings.get("watchlist") or []
                before = len(watchlist)
                watchlist = [
                    w for w in watchlist
                    if not (
                        str(w.get("type", "")).lower() == media_type.lower()
                        and int(w.get("id") or w.get("tmdbId") or 0) == media_id
                    )
                ]
                settings["watchlist"] = watchlist
                _save_settings(settings)
            removed = before - len(watchlist)
            return jsonify({"status": "removed", "removed": removed}), 200

        # ── GET/DELETE /history ───────────────────────────────────────────────
        @bp.route("/history", methods=["GET"])
        def get_history():
            limit = int(request.args.get("limit", 200))
            return jsonify(_load_json(_HISTORY_FILE, [])[:limit])

        @bp.route("/history", methods=["DELETE"])
        def clear_history():
            _save_json(_HISTORY_FILE, [])
            return jsonify({"ok": True})

        # ── Player state + SSE ────────────────────────────────────────────────
        @bp.route("/dashboard/player/state", methods=["GET"])
        def dashboard_player_state():
            return jsonify(_player_state)

        @bp.route("/dashboard/player/events", methods=["GET"])
        def dashboard_player_events():
            q: queue.Queue = queue.Queue(maxsize=10)
            with _sse_lock:
                _sse_queues.append(q)

            def generate():
                yield "data: " + json.dumps(_player_state) + "\n\n"
                try:
                    while True:
                        try:
                            yield q.get(timeout=30)
                        except queue.Empty:
                            yield ": keepalive\n\n"
                except GeneratorExit:
                    pass
                finally:
                    with _sse_lock:
                        if q in _sse_queues:
                            _sse_queues.remove(q)

            return Response(
                stream_with_context(generate()),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # ── UI blueprint (served at /xadarr) ───────────────────────────────────
        ui_bp = Blueprint("xadarr_ui", __name__, url_prefix="/xadarr")

        @ui_bp.route("/", defaults={"path": ""})
        @ui_bp.route("/<path:path>")
        def serve_ui(path):
            if path and os.path.exists(os.path.join(_STATIC_DIR, path)):
                return send_from_directory(_STATIC_DIR, path)
            return send_from_directory(_STATIC_DIR, "index.html")

        # ── Addon manifest blueprint ────────────────────────────────────────────
        # Hosted at /api/addon/xadarr-bridge/manifest.json so the Xadarr app's
        # addon manager can install it via:
        #   http://192.168.254.205:5002/api/addon/xadarr-bridge
        # The xadarr.episeerrSync.syncPrefix field tells CloudSyncRepository which
        # API prefix to use, enabling Episeerr to serve as the sync backend.
        addon_bp = Blueprint("xadarr_addon", __name__, url_prefix="/api/addon/xadarr-bridge")

        @addon_bp.route("/manifest.json", methods=["GET"])
        def addon_manifest():
            host = request.host_url.rstrip("/")
            return jsonify({
                "id":          "episeerr.sync.bridge",
                "name":        "Episeerr Sync Bridge",
                "version":     "1.0.0",
                "description": "Routes Xadarr settings sync and webhooks to this Episeerr instance",
                "logo":        f"{host}/static/logo.png",
                "types":       [],
                "resources":   [],
                "catalogs":    [],
                "xadarr": {
                    "extensions":   ["episeerr_sync"],
                    "episeerr_sync": {
                        "syncPrefix": "/api/integration/xadarr",
                    },
                },
            }), 200

        return [bp, ui_bp, addon_bp]


# ── Module-level instance (auto-discovered by integrations/__init__.py) ───────

integration = XadarrIntegration()

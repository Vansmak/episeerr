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

from flask import Blueprint, jsonify, request, send_from_directory, Response, stream_with_context, redirect, render_template
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
_STATIC_DIR       = os.path.join(os.path.dirname(__file__), "xadarr_static")
_WEB_CONFIG_FILE  = os.path.join(_DATA_DIR, "xadarr_web_config.json")
_WEBHOOK_LOG_FILE = os.path.join(_DATA_DIR, "xadarr_webhook_log.json")
_LOCK = threading.Lock()

# ── Blob (xadarr_settings.json) helpers ───────────────────────────────────────

_XW_PROFILE = "default"
_XW_SYNTHETIC_CATS = [
    {"id": "continue_watching", "title": "Continue Watching", "kind": "STANDARD", "sourceType": "PREINSTALLED"},
    {"id": "watchlist",         "title": "Watchlist",         "kind": "STANDARD", "sourceType": "PREINSTALLED"},
]
_XW_SYNTHETIC_IDS = {c["id"] for c in _XW_SYNTHETIC_CATS}
_XW_FRANCHISE_KW = {
    "marvel": "7153", "dc universe": "9714", "star wars": "1562",
    "james bond": "83", "harry potter": "116", "jurassic park": "803",
    "mission impossible": "585", "john wick": "199879", "the matrix": "133",
    "alien vs predator": "283", "pirates of the caribbean": "270",
    "terminator": "50969", "lord of the rings": "2382", "x-men": "7194",
    "hunger games": "8374", "avatar": "186574", "dune": "11166",
    "indiana jones": "695", "the godfather": "256", "transformers": "5765",
}
_XW_WEBHOOK_DEFAULTS = {
    "webhook_enabled": False,
    "webhook_urls": [],
    "webhook_interval_seconds": "30",
    "webhook_completion_percent": 80,
    "webhook_headers": {},
    "watchlist_api_enabled": False,
    "watchlist_api_port": "7979",
}
_XW_ALL_EVENTS = ["start", "pause", "resume", "stop", "progress", "watchlist.add", "watchlist.remove"]


def _xw_blob():
    blob = _load_json(_SETTINGS_FILE, {})
    profiles = blob.get("profiles", [])
    if not any(p.get("id") == _XW_PROFILE for p in profiles):
        profiles.insert(0, {"id": _XW_PROFILE, "name": "Default",
                             "avatarColor": 4294901760, "avatarId": 1})
        blob["profiles"] = profiles
        blob.setdefault("activeProfileId", _XW_PROFILE)
    return blob


def _xw_save_blob(blob: dict) -> None:
    _save_json(_SETTINGS_FILE, blob)


def _xw_pid(blob: dict) -> str:
    return blob.get("activeProfileId") or _XW_PROFILE


def _xw_connections(blob: dict) -> list:
    try:
        pid = _xw_pid(blob)
        raw = blob.get("profileSettingsById", {}).get(pid, {}).get("homeServerConnectionJson", "")
        return json.loads(raw).get("connections", []) if raw else []
    except Exception:
        return []


def _xw_set_connections(blob: dict, conns: list) -> None:
    pid = _xw_pid(blob)
    blob.setdefault("profileSettingsById", {}).setdefault(pid, {})
    blob["profileSettingsById"][pid]["homeServerConnectionJson"] = json.dumps({"connections": conns})


def _xw_iptv(blob: dict) -> dict:
    pid = _xw_pid(blob)
    profile_iptv = blob.get("iptvByProfile", {}).get(pid, {})
    m3u = profile_iptv.get("m3uUrl") or blob.get("iptvM3uUrl", "")
    epg = profile_iptv.get("epgUrl") or blob.get("iptvEpgUrl", "")
    return {"m3uUrl": m3u, "epgUrl": epg}


def _xw_set_iptv(blob: dict, m3u: str, epg: str) -> None:
    pid = _xw_pid(blob)
    blob.setdefault("iptvByProfile", {})[pid] = {"m3uUrl": m3u, "epgUrl": epg}
    blob["iptvM3uUrl"] = m3u
    blob["iptvEpgUrl"] = epg


def _xw_addons(blob: dict) -> list:
    pid = _xw_pid(blob)
    return blob.get("addonsByProfile", {}).get(pid, [])


def _xw_set_addons(blob: dict, addons: list) -> None:
    pid = _xw_pid(blob)
    blob.setdefault("addonsByProfile", {})[pid] = addons


def _xw_watchlist(blob: dict) -> list:
    pid = _xw_pid(blob)
    return blob.get("watchlistByProfile", {}).get(pid, [])


def _xw_set_watchlist(blob: dict, items: list) -> None:
    pid = _xw_pid(blob)
    blob.setdefault("watchlistByProfile", {})[pid] = items


def _xw_catalogues(blob: dict) -> list:
    pid = _xw_pid(blob)
    return blob.get("catalogsByProfile", {}).get(pid, [])


def _xw_set_catalogues(blob: dict, cats: list) -> None:
    pid = _xw_pid(blob)
    blob.setdefault("catalogsByProfile", {})[pid] = cats


def _xw_norm_media(mt: str) -> str:
    return "show" if (mt or "").lower() in ("tv", "series", "show") else "movie"


def _xw_watchlist_to_web(items: list) -> list:
    out = []
    for item in items:
        tmdb_id = item.get("tmdbId") or item.get("id")
        out.append({
            "id": tmdb_id, "tmdbId": tmdb_id,
            "title": item.get("title", ""),
            "mediaType": _xw_norm_media(item.get("mediaType", "movie")),
            "image": item.get("posterPath", ""),
            "posterPath": item.get("posterPath", ""),
            "backdropUrl": item.get("backdropPath", ""),
            "addedAt": item.get("addedAt"),
            "inWatchlist": True,
        })
    return out


def _xw_mark_watchlist(items: list) -> list:
    blob = _xw_blob()
    wl = _xw_watchlist(blob)
    wl_ids = {str(w.get("tmdbId") or w.get("id") or "") for w in wl}
    for item in items:
        item["inWatchlist"] = str(item.get("id", "")) in wl_ids
    return items


def _xw_tmdb(path: str, params: dict = None):
    blob = _load_json(_SETTINGS_FILE, {})
    key = blob.get("tmdb_api_key", "")
    if not key:
        try:
            from settings_db import get_service
            svc = get_service("tmdb", "default")
            if svc:
                key = svc.get("api_key", "")
        except Exception:
            pass
    if not key:
        return None
    p = {"api_key": key}
    if params:
        p.update(params)
    try:
        r = _http.get(f"https://api.themoviedb.org/3{path}", params=p, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _xw_map_tmdb(item: dict, media_type: str = None) -> dict:
    mt = media_type or item.get("media_type", "movie")
    return {
        "id": item["id"],
        "title": item.get("title") or item.get("name", ""),
        "overview": item.get("overview", ""),
        "image": ("https://image.tmdb.org/t/p/w342" + item["poster_path"]) if item.get("poster_path") else "",
        "backdropUrl": ("https://image.tmdb.org/t/p/w780" + item["backdrop_path"]) if item.get("backdrop_path") else "",
        "mediaType": "show" if mt == "tv" else "movie",
        "year": (item.get("release_date") or item.get("first_air_date") or "")[:4],
        "rating": item.get("vote_average", 0),
        "popularity": item.get("popularity", 0),
        "inWatchlist": False,
    }


def _xw_row_visibility() -> dict:
    return _load_json(_WEB_CONFIG_FILE, {}).get("web_row_visibility", {})


def _xw_save_row_visibility(v: dict) -> None:
    cfg = _load_json(_WEB_CONFIG_FILE, {})
    cfg["web_row_visibility"] = v
    _save_json(_WEB_CONFIG_FILE, cfg)


def _xw_log_webhook(entry: dict) -> None:
    log = _load_json(_WEBHOOK_LOG_FILE, [])
    log.insert(0, entry)
    _save_json(_WEBHOOK_LOG_FILE, log[:100])


def _xw_frigate_url(blob: dict = None) -> str:
    if blob is None:
        blob = _load_json(_SETTINGS_FILE, {})
    return blob.get("frigate_url", "").rstrip("/")


def _xw_detect_server(server_url: str) -> str:
    try:
        r = _http.get(server_url.rstrip("/") + "/System/Info/Public", timeout=6)
        if r.ok:
            name = r.json().get("ProductName", "")
            return "EMBY" if "emby" in name.lower() else "JELLYFIN"
    except Exception:
        pass
    return "UNKNOWN"


def _xw_auth_jf_emby(server_url: str, username: str, password: str) -> dict:
    url = server_url.rstrip("/") + "/Users/AuthenticateByName"
    headers = {
        "Content-Type": "application/json",
        "X-Emby-Authorization": (
            'MediaBrowser Client="Xadarr", Device="Episeerr", '
            'DeviceId="episeerr-xadarr-web", Version="1.0"'
        ),
    }
    r = _http.post(url, json={"Username": username, "Pw": password}, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    try:
        info = _http.get(server_url.rstrip("/") + "/System/Info/Public", timeout=6).json()
        server_name = info.get("ServerName", "")
    except Exception:
        server_name = ""
    return {
        "accessToken": data["AccessToken"],
        "userId": data["User"]["Id"],
        "userName": data["User"].get("Name", username),
        "serverName": server_name,
        "serverId": data.get("ServerId", ""),
    }


def _xw_test_plex(server_url: str, token: str) -> dict:
    r = _http.get(
        server_url.rstrip("/") + "/identity",
        headers={"X-Plex-Token": token, "Accept": "application/json"},
        timeout=6,
    )
    r.raise_for_status()
    data = r.json().get("MediaContainer", {})
    return {"serverName": data.get("friendlyName", "Plex"), "serverId": data.get("machineIdentifier", "")}

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


def broadcast_episeerr_event(entry: dict) -> None:
    payload = "event: episeerr\ndata: " + json.dumps(entry) + "\n\n"
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
            # Preserve homeServerConnectionJson if the incoming blob has blank tokens.
            # Prevents a device that lost its Keystore key from wiping credentials for all clients.
            existing = _load_json(_SETTINGS_FILE, {})
            existing_profiles = existing.get("profileSettingsById") or {}
            incoming_profiles = body.get("profileSettingsById") or {}
            for pid, existing_ps in existing_profiles.items():
                existing_conn = existing_ps.get("homeServerConnectionJson") if isinstance(existing_ps, dict) else None
                if not existing_conn:
                    continue
                incoming_ps = incoming_profiles.get(pid)
                if not isinstance(incoming_ps, dict):
                    continue
                incoming_conn = incoming_ps.get("homeServerConnectionJson")
                if not incoming_conn:
                    incoming_ps["homeServerConnectionJson"] = existing_conn
                else:
                    try:
                        conns = json.loads(incoming_conn).get("connections", [])
                        if conns and not any(c.get("accessToken") for c in conns):
                            incoming_ps["homeServerConnectionJson"] = existing_conn
                    except Exception:
                        pass
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

        @ui_bp.route("/")
        @ui_bp.route("/home")
        def xadarr_embed_home():
            return render_template("xadarr_embed.html", section="home")

        @ui_bp.route("/discover")
        def xadarr_embed_discover():
            return render_template("xadarr_embed.html", section="discover")

        @ui_bp.route("/search")
        def xadarr_embed_search():
            return render_template("xadarr_embed.html", section="search")

        @ui_bp.route("/cameras")
        def xadarr_embed_cameras():
            return render_template("xadarr_embed.html", section="cameras")

        @ui_bp.route("/settings")
        def xadarr_embed_settings():
            return render_template("xadarr_embed.html", section="settings")

        @ui_bp.route("/<path:path>")
        def serve_ui(path):
            if os.path.exists(os.path.join(_STATIC_DIR, path)):
                return send_from_directory(_STATIC_DIR, path)
            return redirect("/xadarr/home")

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

        # ── Dispatcharr bridge addon manifest ──────────────────────────────────
        # Replaces http://<xadarr-server>:7979/dispatcharr-bridge/manifest.json.
        # Install in Xadarr app from: http://<episeerr>:5002/api/addon/dispatcharr-bridge
        dc_bp = Blueprint("dispatcharr_addon", __name__, url_prefix="/api/addon/dispatcharr-bridge")

        @dc_bp.route("/manifest.json", methods=["GET"])
        def dispatcharr_manifest():
            return jsonify({
                "id":          "com.joe.dispatcharr-bridge",
                "version":     "1.0.0",
                "name":        "Dispatcharr Bridge",
                "description": "Dispatcharr integration for IPTV group management",
                "catalogs":    [],
                "resources":   [],
                "xadarr": {
                    "extensions":    ["group_blacklist"],
                    "groupBlacklist": {
                        "defaultPath": "/data/dispatcharr_blacklist.txt",
                    },
                },
            }), 200

        # ── Web UI API blueprint (mirrors xadarr-server API at /xadarr/api) ──────
        web_bp = Blueprint("xadarr_web", __name__, url_prefix="/xadarr/api")

        # ── Settings ─────────────────────────────────────────────────────────
        @web_bp.route("/settings", methods=["GET"])
        def web_get_settings():
            blob = _load_json(_SETTINGS_FILE, {})
            for k, v in _XW_WEBHOOK_DEFAULTS.items():
                blob.setdefault(k, v)
            return jsonify(blob)

        @web_bp.route("/settings", methods=["POST"])
        def web_post_settings():
            data = request.get_json(force=True) or {}
            existing = _load_json(_SETTINGS_FILE, {})
            existing.update(data)
            _save_json(_SETTINGS_FILE, existing)
            return jsonify({"ok": True})

        # ── Setup: servers ────────────────────────────────────────────────────
        @web_bp.route("/setup/servers", methods=["GET"])
        def web_get_servers():
            blob = _xw_blob()
            servers = _xw_connections(blob)
            safe = [{k: v for k, v in s.items() if k not in ("accessToken", "accountToken")}
                    for s in servers]
            return jsonify(safe)

        @web_bp.route("/setup/servers/connect", methods=["POST"])
        def web_connect_server():
            body = request.get_json(force=True) or {}
            kind = body.get("kind", "").upper()
            server_url = body.get("url", "").rstrip("/")
            display_name = body.get("displayName", "")
            if not server_url:
                return jsonify({"error": "url required"}), 400
            try:
                if kind in ("JELLYFIN", "EMBY", ""):
                    username = body.get("username", "")
                    password = body.get("password", "")
                    if not kind:
                        kind = _xw_detect_server(server_url)
                    if kind == "UNKNOWN":
                        return jsonify({"error": "Could not detect server type — specify kind"}), 400
                    auth = _xw_auth_jf_emby(server_url, username, password)
                    conn = {
                        "enabled": True,
                        "connectionId": f"{kind}:{server_url}:{auth['userId']}",
                        "serverUrl": server_url,
                        "displayName": display_name or auth["serverName"] or server_url,
                        "serverName": auth["serverName"],
                        "serverKind": kind,
                        "serverId": auth["serverId"],
                        "userId": auth["userId"],
                        "userName": auth["userName"],
                        "accessToken": auth["accessToken"],
                        "accountToken": "",
                        "collections": [],
                        "lastConnectedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
                    }
                elif kind == "PLEX":
                    token = body.get("token", "")
                    if not token:
                        return jsonify({"error": "token required for Plex"}), 400
                    info = _xw_test_plex(server_url, token)
                    conn = {
                        "enabled": True,
                        "connectionId": f"PLEX:{server_url}",
                        "serverUrl": server_url,
                        "displayName": display_name or info["serverName"] or server_url,
                        "serverName": info["serverName"],
                        "serverKind": "PLEX",
                        "serverId": info["serverId"],
                        "userId": "", "userName": "",
                        "accessToken": token, "accountToken": token,
                        "collections": [],
                        "lastConnectedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
                    }
                else:
                    return jsonify({"error": f"Unknown kind: {kind}"}), 400

                blob = _xw_blob()
                conns = [c for c in _xw_connections(blob) if c.get("connectionId") != conn["connectionId"]]
                conns.append(conn)
                _xw_set_connections(blob, conns)
                _xw_save_blob(blob)
                safe = {k: v for k, v in conn.items() if k not in ("accessToken", "accountToken")}
                return jsonify({"ok": True, "connection": safe})
            except Exception as exc:
                return jsonify({"error": str(exc)}), 500

        @web_bp.route("/setup/servers/<connection_id>", methods=["PATCH"])
        def web_rename_server(connection_id):
            body = request.get_json(force=True) or {}
            blob = _xw_blob()
            conns = _xw_connections(blob)
            for c in conns:
                if c.get("connectionId") == connection_id:
                    c["displayName"] = body.get("displayName", "").strip()
                    break
            _xw_set_connections(blob, conns)
            _xw_save_blob(blob)
            return jsonify({"ok": True})

        @web_bp.route("/setup/servers/<connection_id>", methods=["DELETE"])
        def web_delete_server(connection_id):
            blob = _xw_blob()
            conns = [c for c in _xw_connections(blob) if c.get("connectionId") != connection_id]
            _xw_set_connections(blob, conns)
            _xw_save_blob(blob)
            return jsonify({"ok": True})

        # ── Setup: IPTV ───────────────────────────────────────────────────────
        @web_bp.route("/setup/iptv", methods=["GET"])
        def web_get_iptv():
            return jsonify(_xw_iptv(_xw_blob()))

        @web_bp.route("/setup/iptv", methods=["POST"])
        def web_save_iptv():
            body = request.get_json(force=True) or {}
            blob = _xw_blob()
            _xw_set_iptv(blob, body.get("m3uUrl", ""), body.get("epgUrl", ""))
            _xw_save_blob(blob)
            return jsonify({"ok": True})

        # ── Setup: addons ─────────────────────────────────────────────────────
        @web_bp.route("/setup/addons", methods=["GET"])
        def web_get_addons():
            return jsonify(_xw_addons(_xw_blob()))

        @web_bp.route("/setup/addons", methods=["POST"])
        def web_add_addon():
            body = request.get_json(force=True) or {}
            manifest_url = body.get("url", "").strip().rstrip("/")
            if not manifest_url:
                return jsonify({"error": "url required"}), 400
            fetch_url = manifest_url if manifest_url.endswith("manifest.json") else manifest_url + "/manifest.json"
            try:
                r = _http.get(fetch_url, timeout=10)
                r.raise_for_status()
                manifest = r.json()
            except Exception as exc:
                return jsonify({"error": f"Could not fetch manifest: {exc}"}), 400
            addon = {
                "id": manifest.get("id", manifest_url),
                "name": manifest.get("name", "Unknown"),
                "version": manifest.get("version", "0.0.1"),
                "description": manifest.get("description", ""),
                "isInstalled": True, "isEnabled": True,
                "type": "COMMUNITY", "runtimeKind": "STREMIO",
                "installSource": "DIRECT_URL",
                "url": manifest_url, "logo": manifest.get("logo"),
                "transportUrl": manifest_url,
            }
            blob = _xw_blob()
            addons = [a for a in _xw_addons(blob) if a.get("id") != addon["id"]]
            addons.append(addon)
            _xw_set_addons(blob, addons)
            _xw_save_blob(blob)
            return jsonify({"ok": True, "addon": addon})

        @web_bp.route("/setup/addons/<path:addon_id>", methods=["DELETE"])
        def web_delete_addon(addon_id):
            blob = _xw_blob()
            _xw_set_addons(blob, [a for a in _xw_addons(blob) if a.get("id") != addon_id])
            _xw_save_blob(blob)
            return jsonify({"ok": True})

        # ── Catalogues ────────────────────────────────────────────────────────
        @web_bp.route("/catalogues", methods=["GET"])
        def web_get_catalogues():
            blob = _xw_blob()
            cats = _xw_catalogues(blob)
            visibility = _xw_row_visibility()
            synthetic = []
            for i, sc in enumerate(_XW_SYNTHETIC_CATS):
                hidden = visibility.get(sc["id"], False)
                sort_order = visibility.get(f"{sc['id']}_sort", i - len(_XW_SYNTHETIC_CATS))
                synthetic.append({**sc, "isHidden": hidden,
                                   "placement": "HIDDEN" if hidden else "HOME",
                                   "sortOrder": sort_order})
            for i, c in enumerate(cats):
                if c.get("sortOrder") is None:
                    c["sortOrder"] = i
            merged = synthetic + cats
            merged.sort(key=lambda c: (c.get("sortOrder") or 0))
            return jsonify(merged)

        @web_bp.route("/catalogues", methods=["PUT"])
        def web_put_catalogues():
            cats = request.get_json(force=True) or []
            synthetic = [c for c in cats if c.get("id") in _XW_SYNTHETIC_IDS]
            real = [c for c in cats if c.get("id") not in _XW_SYNTHETIC_IDS]
            if synthetic:
                v = _xw_row_visibility()
                for sc in synthetic:
                    v[sc["id"]] = sc.get("placement") == "HIDDEN" or bool(sc.get("isHidden"))
                    v[f"{sc['id']}_sort"] = sc.get("sortOrder", 0)
                _xw_save_row_visibility(v)
            blob = _xw_blob()
            _xw_set_catalogues(blob, real)
            _xw_save_blob(blob)
            return jsonify({"ok": True})

        # ── Trakt ─────────────────────────────────────────────────────────────
        @web_bp.route("/trakt/status", methods=["GET"])
        def web_trakt_status():
            blob = _xw_blob()
            tokens = (blob.get("traktTokens") or {}).get(_XW_PROFILE, {})
            client_id = blob.get("trakt_client_id", "")
            connected = bool(tokens.get("accessToken"))
            return jsonify({
                "connected": connected,
                "hasClientId": bool(client_id),
                "clientIdHint": (client_id[:6] + "…") if client_id else "",
            })

        @web_bp.route("/trakt/connect", methods=["GET"])
        def web_trakt_connect():
            blob = _xw_blob()
            client_id = blob.get("trakt_client_id", "")
            if not client_id:
                return "No Trakt Client ID configured — add it in Settings first", 400
            redirect_uri = request.host_url.rstrip("/") + "/xadarr/api/trakt/callback"
            auth_url = (
                "https://trakt.tv/oauth/authorize"
                f"?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
            )
            return redirect(auth_url)

        @web_bp.route("/trakt/callback", methods=["GET"])
        def web_trakt_callback():
            code = request.args.get("code", "")
            if not code:
                return "No code received from Trakt", 400
            blob = _xw_blob()
            client_id = blob.get("trakt_client_id", "")
            client_secret = blob.get("trakt_client_secret", "")
            redirect_uri = request.host_url.rstrip("/") + "/xadarr/api/trakt/callback"
            try:
                r = _http.post(
                    "https://api.trakt.tv/oauth/token",
                    json={
                        "code": code, "client_id": client_id, "client_secret": client_secret,
                        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=15,
                )
                r.raise_for_status()
                tok = r.json()
                import time as _time
                blob.setdefault("traktTokens", {})[_XW_PROFILE] = {
                    "accessToken": tok.get("access_token"),
                    "refreshToken": tok.get("refresh_token"),
                    "expiresAt": int(_time.time() * 1000) + tok.get("expires_in", 0) * 1000,
                }
                blob["traktLinked"] = True
                _xw_save_blob(blob)
                return redirect("/xadarr?trakt=connected")
            except Exception as exc:
                return f"Trakt token exchange failed: {exc}", 502

        @web_bp.route("/trakt/disconnect", methods=["POST"])
        def web_trakt_disconnect():
            blob = _xw_blob()
            blob.setdefault("traktTokens", {})[_XW_PROFILE] = {}
            blob["traktLinked"] = False
            _xw_save_blob(blob)
            return jsonify({"ok": True})

        # ── Watchlist ─────────────────────────────────────────────────────────
        @web_bp.route("/media/watchlist", methods=["GET"])
        def web_get_watchlist():
            blob = _xw_blob()
            return jsonify(_xw_watchlist_to_web(_xw_watchlist(blob)))

        @web_bp.route("/media/watchlist", methods=["POST"])
        def web_add_watchlist():
            item = request.get_json(force=True) or {}
            tmdb_id = item.get("id") or item.get("tmdbId")
            if not tmdb_id:
                return jsonify({"error": "missing id"}), 400
            tmdb_id = int(tmdb_id)
            media_type = _xw_norm_media(item.get("mediaType") or "movie")
            poster_path = item.get("posterPath") or item.get("image") or ""
            backdrop_path = item.get("backdropPath") or item.get("backdropUrl") or ""
            title = item.get("title", "")
            if not poster_path:
                endpoint = "/movie/" if media_type == "movie" else "/tv/"
                tmdb_data = _xw_tmdb(endpoint + str(tmdb_id))
                if tmdb_data:
                    if tmdb_data.get("poster_path"):
                        poster_path = "https://image.tmdb.org/t/p/w342" + tmdb_data["poster_path"]
                    if tmdb_data.get("backdrop_path"):
                        backdrop_path = "https://image.tmdb.org/t/p/w780" + tmdb_data["backdrop_path"]
                    if not title:
                        title = tmdb_data.get("title") or tmdb_data.get("name") or ""
            blob = _xw_blob()
            wl = _xw_watchlist(blob)
            if not any(int(w.get("tmdbId") or w.get("id") or 0) == tmdb_id for w in wl):
                import time as _time
                new_item = {
                    "tmdbId": tmdb_id, "title": title, "mediaType": media_type,
                    "posterPath": poster_path, "backdropPath": backdrop_path,
                    "addedAt": int(_time.time() * 1000), "sourceOrder": 0,
                }
                wl.insert(0, new_item)
                _xw_set_watchlist(blob, wl)
                _xw_save_blob(blob)
            return jsonify({"ok": True})

        @web_bp.route("/media/watchlist/<media_type>/<int:item_id>", methods=["DELETE"])
        def web_delete_watchlist(media_type, item_id):
            blob = _xw_blob()
            wl = [w for w in _xw_watchlist(blob) if int(w.get("tmdbId") or w.get("id") or 0) != item_id]
            _xw_set_watchlist(blob, wl)
            _xw_save_blob(blob)
            return jsonify({"ok": True})

        # ── Continue watching ─────────────────────────────────────────────────
        @web_bp.route("/media/continue-watching", methods=["GET"])
        def web_continue_watching():
            blob = _xw_blob()
            pid = _xw_pid(blob)
            items = blob.get("localContinueWatchingByProfile", {}).get(pid, [])
            result = [{
                "id": it.get("id"), "title": it.get("title", ""),
                "episode": it.get("episode"), "episodeTitle": it.get("episodeTitle", ""),
                "season": it.get("season"), "mediaType": it.get("mediaType", ""),
                "image": it.get("posterPath", ""), "backdropUrl": it.get("backdropPath", ""),
                "progress": it.get("progress", 0), "durationSeconds": it.get("durationSeconds", 0),
                "resumePositionSeconds": it.get("resumePositionSeconds", 0),
            } for it in items]
            return jsonify(result)

        # ── History ───────────────────────────────────────────────────────────
        @web_bp.route("/media/history", methods=["GET"])
        def web_get_history():
            limit = int(request.args.get("limit", 50))
            return jsonify(_load_json(_HISTORY_FILE, [])[:limit])

        @web_bp.route("/media/history", methods=["DELETE"])
        def web_clear_history():
            _save_json(_HISTORY_FILE, [])
            return jsonify({"ok": True})

        # ── TMDB: detail / search / discover / trending / popular / upcoming ──
        @web_bp.route("/media/detail", methods=["GET"])
        def web_media_detail():
            tmdb_id = request.args.get("id", "").strip()
            media_type = request.args.get("type", "movie").strip()
            if not tmdb_id:
                return jsonify({"error": "id required"}), 400
            endpoint = f"/tv/{tmdb_id}" if media_type == "tv" else f"/movie/{tmdb_id}"
            data = _xw_tmdb(endpoint, {"append_to_response": "credits"})
            if not data:
                return jsonify({"error": "not found"}), 404
            item = _xw_map_tmdb(data, media_type)
            item["genres"] = [g["name"] for g in data.get("genres", [])]
            item["runtime"] = data.get("runtime") or (data.get("episode_run_time") or [None])[0]
            item["tagline"] = data.get("tagline", "")
            item["status"] = data.get("status", "")
            item["cast"] = [c["name"] for c in data.get("credits", {}).get("cast", [])[:6]]
            return jsonify(_xw_mark_watchlist([item])[0])

        @web_bp.route("/media/search", methods=["GET"])
        def web_media_search():
            q = request.args.get("q", "").strip()
            if len(q) < 2:
                return jsonify([])
            data = _xw_tmdb("/search/multi", {"query": q, "page": 1})
            if not data:
                return jsonify({"error": "TMDB not configured"}), 503
            items = [_xw_map_tmdb(r) for r in data.get("results", [])
                     if r.get("media_type") in ("movie", "tv") and r.get("poster_path")]
            return jsonify(_xw_mark_watchlist(items))

        @web_bp.route("/media/discover", methods=["GET"])
        def web_media_discover():
            provider_id = request.args.get("provider_id", "").strip()
            genre_id    = request.args.get("genre_id",    "").strip()
            year_start  = request.args.get("year_start",  "").strip()
            year_end    = request.args.get("year_end",    "").strip()
            franchise   = request.args.get("franchise",   "").strip().lower()
            if not provider_id and not genre_id and not year_start and not franchise:
                return jsonify({"movies": [], "shows": []})
            if franchise:
                kw_id = _XW_FRANCHISE_KW.get(franchise)
                if not kw_id:
                    return jsonify({"movies": [], "shows": []})
                data = _xw_tmdb("/discover/movie", {"with_keywords": kw_id, "sort_by": "popularity.desc", "page": 1}) or {}
                movies = [_xw_map_tmdb(r, "movie") for r in data.get("results", []) if r.get("poster_path")][:20]
                return jsonify({"movies": _xw_mark_watchlist(movies), "shows": []})
            tv_genre_map = {"28": "10759", "14": "10765", "878": "10765", "10752": "10768"}
            base_m = {"sort_by": "popularity.desc", "vote_count.gte": 20}
            base_t = {"sort_by": "popularity.desc", "vote_count.gte": 20}
            if provider_id:
                base_m.update({"with_watch_providers": provider_id, "watch_region": "US"})
                base_t.update({"with_watch_providers": provider_id, "watch_region": "US"})
            if genre_id:
                base_m["with_genres"] = genre_id
                base_t["with_genres"] = tv_genre_map.get(genre_id, genre_id)
            if year_start and year_end:
                base_m.update({"primary_release_date.gte": f"{year_start}-01-01", "primary_release_date.lte": f"{year_end}-12-31"})
                base_t.update({"first_air_date.gte": f"{year_start}-01-01", "first_air_date.lte": f"{year_end}-12-31"})
            def _pages(endpoint, params, pages=3):
                seen, results = set(), []
                for p in range(1, pages + 1):
                    data = _xw_tmdb(endpoint, {**params, "page": p}) or {}
                    for r in data.get("results", []):
                        if r.get("poster_path") and r["id"] not in seen:
                            seen.add(r["id"]); results.append(r)
                    if p >= (data.get("total_pages") or 1):
                        break
                return results
            movies = [_xw_map_tmdb(r, "movie") for r in _pages("/discover/movie", base_m)]
            shows  = [_xw_map_tmdb(r, "tv")    for r in _pages("/discover/tv",    base_t)]
            return jsonify({"movies": _xw_mark_watchlist(movies), "shows": _xw_mark_watchlist(shows)})

        @web_bp.route("/media/search-discover", methods=["GET"])
        def web_search_discover():
            from datetime import timedelta
            type_filter = request.args.get("type", "all").strip()
            genre_id    = request.args.get("genre_id", "").strip()
            today            = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            one_year_ago     = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
            three_months_ago = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
            tv_genre_map = {"28": "10759", "14": "10765", "878": "10765", "10752": "10768"}
            movie_genre = genre_id or None
            tv_genre    = tv_genre_map.get(genre_id, genre_id) if genre_id else None

            def _fetch(endpoint, sort_by, extra):
                data = _xw_tmdb(endpoint, {"sort_by": sort_by, "page": 1, **extra}) or {}
                return [r for r in data.get("results", []) if r.get("poster_path")][:20]

            def fm(sort_by, extra):
                return _fetch("/discover/movie", sort_by, {**extra, **({"with_genres": movie_genre} if movie_genre else {})})

            def ft(sort_by, extra):
                return _fetch("/discover/tv", sort_by, {**extra, **({"with_genres": tv_genre} if tv_genre else {})})

            def build(sort_by, m_extra, t_extra):
                if type_filter == "movies":
                    return [_xw_map_tmdb(r, "movie") for r in fm(sort_by, m_extra)]
                if type_filter == "tv":
                    return [_xw_map_tmdb(r, "tv") for r in ft(sort_by, t_extra)]
                m = [_xw_map_tmdb(r, "movie") for r in fm(sort_by, m_extra)]
                t = [_xw_map_tmdb(r, "tv")    for r in ft(sort_by, t_extra)]
                out = []
                for i in range(max(len(m), len(t))):
                    if i < len(m): out.append(m[i])
                    if i < len(t): out.append(t[i])
                return out[:20]

            result = {
                "trending":          build("popularity.desc",  {"vote_count.gte": 50,   "primary_release_date.lte": today}, {"vote_count.gte": 50,   "first_air_date.lte": today}),
                "popular_this_year": build("popularity.desc",  {"vote_count.gte": 20,   "primary_release_date.gte": one_year_ago, "primary_release_date.lte": today}, {"vote_count.gte": 20, "first_air_date.gte": one_year_ago, "first_air_date.lte": today}),
                "top_rated":         build("vote_average.desc",{"vote_count.gte": 1000, "primary_release_date.lte": today}, {"vote_count.gte": 1000, "first_air_date.lte": today}),
                "new_releases":      build("popularity.desc",  {"vote_count.gte": 10,   "primary_release_date.gte": three_months_ago, "primary_release_date.lte": today}, {"vote_count.gte": 10, "first_air_date.gte": three_months_ago, "first_air_date.lte": today}),
                "hidden_gems":       build("vote_average.desc",{"vote_count.gte": 200,  "vote_count.lte": 5000, "primary_release_date.lte": today}, {"vote_count.gte": 200, "vote_count.lte": 5000, "first_air_date.lte": today}),
            }
            for arr in result.values():
                _xw_mark_watchlist(arr)
            return jsonify(result)

        @web_bp.route("/media/trending", methods=["GET"])
        def web_media_trending():
            movies = _xw_tmdb("/trending/movie/week") or {}
            shows  = _xw_tmdb("/trending/tv/week")    or {}
            items = (
                [_xw_map_tmdb(r, "movie") for r in movies.get("results", []) if r.get("poster_path")]
                + [_xw_map_tmdb(r, "tv") for r in shows.get("results", []) if r.get("poster_path")]
            )
            items.sort(key=lambda x: x["popularity"], reverse=True)
            return jsonify(_xw_mark_watchlist(items[:40]))

        @web_bp.route("/media/popular", methods=["GET"])
        def web_media_popular():
            movies = _xw_tmdb("/movie/popular") or {}
            shows  = _xw_tmdb("/tv/popular")    or {}
            return jsonify({
                "movies": _xw_mark_watchlist([_xw_map_tmdb(r, "movie") for r in movies.get("results", []) if r.get("poster_path")]),
                "shows":  _xw_mark_watchlist([_xw_map_tmdb(r, "tv")    for r in shows.get("results",  []) if r.get("poster_path")]),
            })

        @web_bp.route("/media/upcoming", methods=["GET"])
        def web_media_upcoming():
            data = _xw_tmdb("/movie/upcoming") or {}
            items = [_xw_map_tmdb(r, "movie") for r in data.get("results", []) if r.get("poster_path")]
            return jsonify(_xw_mark_watchlist(items))

        # ── Server items (Jellyfin / Emby / Plex) ────────────────────────────
        @web_bp.route("/media/server-items", methods=["GET"])
        def web_server_items():
            blob = _xw_blob()
            connections = [c for c in _xw_connections(blob) if c.get("enabled")]
            if not connections:
                return jsonify([])
            conn = connections[0]
            kind       = conn.get("serverKind", "JELLYFIN")
            server_url = conn.get("serverUrl", "").rstrip("/")
            token      = conn.get("accessToken", "")
            user_id    = conn.get("userId", "")
            if kind in ("JELLYFIN", "EMBY"):
                try:
                    r = _http.get(
                        f"{server_url}/Users/{user_id}/Items",
                        params={"SortBy": "DateCreated", "SortOrder": "Descending",
                                "IncludeItemTypes": "Movie,Series", "Recursive": "true",
                                "Fields": "Overview,ProviderIds,PrimaryImageAspectRatio",
                                "ImageTypeLimit": "1", "EnableImageTypes": "Primary,Backdrop", "Limit": "20"},
                        headers={"X-Emby-Token": token, "Accept": "application/json"},
                        timeout=10,
                    )
                    r.raise_for_status()
                    items = []
                    for it in r.json().get("Items", []):
                        iid = it.get("Id", "")
                        tmdb_id = it.get("ProviderIds", {}).get("Tmdb") or iid
                        items.append({
                            "id": tmdb_id, "title": it.get("Name", ""),
                            "mediaType": "movie" if it.get("Type") == "Movie" else "show",
                            "image": f"/xadarr/api/media/jf-image/{iid}/Primary" if iid else "",
                            "backdropUrl": f"/xadarr/api/media/jf-image/{iid}/Backdrop" if iid else "",
                            "overview": it.get("Overview", ""), "year": it.get("ProductionYear", ""),
                            "inWatchlist": False,
                        })
                    return jsonify(_xw_mark_watchlist(items))
                except Exception as exc:
                    return jsonify({"error": str(exc)}), 502
            elif kind == "PLEX":
                try:
                    r = _http.get(f"{server_url}/library/recentlyAdded",
                                  params={"X-Plex-Token": token},
                                  headers={"Accept": "application/json"}, timeout=10)
                    r.raise_for_status()
                    media_list = r.json().get("MediaContainer", {}).get("Metadata", [])[:20]
                    items = []
                    for it in media_list:
                        guids = it.get("Guid", [])
                        tmdb_id = next((g["id"].split("//")[-1] for g in guids if "tmdb" in g.get("id", "")), it.get("ratingKey", ""))
                        thumb = it.get("thumb", "")
                        art   = it.get("art", "")
                        items.append({
                            "id": tmdb_id, "title": it.get("title", ""),
                            "mediaType": "movie" if it.get("type") == "movie" else "show",
                            "image": f"{server_url}{thumb}?X-Plex-Token={token}" if thumb else "",
                            "backdropUrl": f"{server_url}{art}?X-Plex-Token={token}" if art else "",
                            "overview": it.get("summary", ""), "year": it.get("year", ""),
                            "inWatchlist": False,
                        })
                    return jsonify(_xw_mark_watchlist(items))
                except Exception as exc:
                    return jsonify({"error": str(exc)}), 502
            return jsonify([])

        @web_bp.route("/media/jf-image/<item_id>/<image_type>", methods=["GET"])
        def web_jf_image(item_id, image_type):
            blob = _xw_blob()
            conn = next((c for c in _xw_connections(blob)
                         if c.get("enabled") and c.get("serverKind") in ("JELLYFIN", "EMBY")), None)
            if not conn:
                return "No server", 404
            server_url = conn.get("serverUrl", "").rstrip("/")
            token = conn.get("accessToken", "")
            size = "maxHeight=300" if image_type == "Primary" else "maxHeight=500"
            url = f"{server_url}/Items/{item_id}/Images/{image_type}?{size}&api_key={token}"
            try:
                r = _http.get(url, timeout=8)
                r.raise_for_status()
                resp = Response(r.content, content_type=r.headers.get("Content-Type", "image/jpeg"))
                resp.headers["Cache-Control"] = "public, max-age=86400"
                return resp
            except Exception:
                return "Image not found", 404

        # ── Cameras (Frigate) ─────────────────────────────────────────────────
        @web_bp.route("/cameras/list", methods=["GET"])
        def web_cameras_list():
            blob = _xw_blob()
            frigate_url = _xw_frigate_url(blob)
            if not frigate_url:
                return jsonify([])
            try:
                r = _http.get(f"{frigate_url}/api/config", timeout=8)
                r.raise_for_status()
                cameras = [
                    {"name": name, "snapshotUrl": f"/xadarr/api/cameras/snapshot/{name}"}
                    for name in r.json().get("cameras", {}).keys()
                ]
                return jsonify(cameras)
            except Exception as exc:
                return jsonify({"error": str(exc)}), 502

        @web_bp.route("/cameras/snapshot/<camera_name>", methods=["GET"])
        def web_camera_snapshot(camera_name):
            frigate_url = _xw_frigate_url()
            if not frigate_url:
                return "Frigate not configured", 503
            try:
                r = _http.get(f"{frigate_url}/api/{camera_name}/latest.jpg", timeout=5)
                r.raise_for_status()
                return Response(r.content, content_type=r.headers.get("Content-Type", "image/jpeg"))
            except Exception as exc:
                return str(exc), 502

        # ── Episeerr integration ───────────────────────────────────────────────
        @web_bp.route("/episeerr/pending", methods=["GET"])
        def web_episeerr_pending():
            try:
                from settings_db import get_all_pending_requests
                rows = get_all_pending_requests()
            except Exception:
                return jsonify([]), 200
            return jsonify([{
                "id": r.get("id"), "seriesId": r.get("series_id"),
                "title": r.get("title", ""), "tmdbId": r.get("tmdb_id"),
                "tvdbId": r.get("tvdb_id"), "poster": None,
                "createdAt": r.get("created_at"),
            } for r in rows]), 200

        @web_bp.route("/episeerr/rules", methods=["GET"])
        def web_episeerr_rules():
            try:
                from episeerr import load_config
                config = load_config()
                rules = config.get("rules", {})
                result = [{"id": k, "name": v.get("display_name") or k.replace("_", " ").title(),
                           "description": v.get("description", "")}
                          for k, v in rules.items()]
                result.sort(key=lambda x: x["name"])
                return jsonify(result)
            except Exception:
                return jsonify([]), 200

        @web_bp.route("/episeerr/assign", methods=["POST"])
        def web_episeerr_assign():
            body = request.get_json(force=True) or {}
            tmdb_id  = str(body.get("tmdb_id", "")).strip()
            rule_id  = str(body.get("rule_id", "")).strip()
            if not tmdb_id or not rule_id:
                return jsonify({"success": False, "error": "tmdb_id and rule_id required"}), 400
            try:
                from episeerr import load_config, save_config
                from settings_db import find_pending_request_by_tmdb
                config = load_config()
                if rule_id not in config.get("rules", {}):
                    return jsonify({"success": False, "error": f"Unknown rule: {rule_id}"}), 400
                req = find_pending_request_by_tmdb(tmdb_id)
                if not req:
                    return jsonify({"success": False, "error": "No pending request found"}), 404
                series_id = str(req.get("series_id", ""))
                if not series_id:
                    return jsonify({"success": False, "error": "No series_id on pending request"}), 400
                config["rules"][rule_id].setdefault("series", {})[series_id] = {"activity_date": None}
                save_config(config)
                return jsonify({"success": True})
            except Exception as exc:
                return jsonify({"success": False, "error": str(exc)}), 500

        # ── Webhook test ─────────────────────────────────────────────────────
        @web_bp.route("/webhook/test", methods=["POST"])
        def web_webhook_test():
            body = request.get_json(force=True) or {}
            target_url = body.get("url", "").strip()
            events = body.get("events") or _XW_ALL_EVENTS
            if not target_url:
                return jsonify({"ok": False, "error": "No URL specified"}), 400
            blob = _load_json(_SETTINGS_FILE, {})
            req_headers = {"Content-Type": "application/json", **(blob.get("webhook_headers") or {})}
            event_name = events[0] if events else "start"
            payload = {"event": event_name, "title": "Test Event", "media_type": "episode", "progress_percent": 0}
            try:
                r = _http.post(target_url, json=payload, headers=req_headers, timeout=5)
                _xw_log_webhook({"timestamp": datetime.now(timezone.utc).isoformat(),
                                  "event": event_name, "url": target_url,
                                  "status_code": r.status_code, "success": r.ok, "error": None})
                return jsonify({"ok": r.ok, "status_code": r.status_code})
            except Exception as exc:
                _xw_log_webhook({"timestamp": datetime.now(timezone.utc).isoformat(),
                                  "event": event_name, "url": target_url,
                                  "status_code": None, "success": False, "error": str(exc)})
                return jsonify({"ok": False, "error": str(exc)})

        @web_bp.route("/webhook/log", methods=["GET"])
        def web_webhook_log():
            return jsonify(_load_json(_WEBHOOK_LOG_FILE, [])[:20])

        # ── Player state + SSE ────────────────────────────────────────────────
        @web_bp.route("/player/state", methods=["GET"])
        def web_player_state():
            return jsonify(_player_state)

        @web_bp.route("/player/events", methods=["GET"])
        def web_player_events():
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

        return [bp, ui_bp, addon_bp, dc_bp, web_bp]


# ── Module-level instance (auto-discovered by integrations/__init__.py) ───────

integration = XadarrIntegration()

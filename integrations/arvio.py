"""
Arvio Integration for Episeerr
───────────────────────────────
Provides:   Settings sync, playback webhooks, watchlist enrichment,
            and a web dashboard at /arvio for Arvio Android TV app.

Sync API (prefix: /api/integration/arvio):
    GET  /status      ← Arvio pings this to verify the server is reachable
    GET  /settings    ← Arvio pulls its full settings blob on new-device setup
    PUT  /settings    ← Arvio pushes its settings blob (profile sync, addons, etc.)
    POST /webhook     ← Arvio posts playback events (start/pause/stop/progress)
    GET  /watchlist   ← Arvio fetches its watchlist enriched with Sonarr/Radarr status
    POST /sync        ← Arvio triggers a watchlist sync to Sonarr/Radarr

Dashboard API (also under /api/integration/arvio):
    GET/DELETE /history              ← playback event log
    GET/POST   /dashboard/watchlist  ← dashboard-managed watchlist
    DELETE     /dashboard/watchlist/<type>/<id>
    GET        /dashboard/search?q= ← TMDB search (uses Episeerr's TMDB key)
    GET        /dashboard/trending   ← TMDB trending
    GET        /dashboard/player/state
    GET        /dashboard/player/events  ← SSE stream

Dashboard UI (prefix: /arvio):
    GET /         ← SPA index.html
    GET /static/* ← JS / CSS

Data files:
    data/arvio_settings.json        ← settings blob synced from TV app
    data/arvio_history.json         ← playback event log (last 500)
    data/arvio_dashboard_watchlist.json ← dashboard-managed watchlist
"""

import os
import json
import queue
import logging
import subprocess
import threading
import time
import requests as _requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request, send_from_directory, Response, stream_with_context, current_app
from integrations.base import ServiceIntegration

logger = logging.getLogger(__name__)

# ── File paths ────────────────────────────────────────────────────────────────

_DATA_DIR = os.path.join(os.getcwd(), "data")
_SETTINGS_FILE  = os.path.join(_DATA_DIR, "arvio_settings.json")
_HISTORY_FILE   = os.path.join(_DATA_DIR, "arvio_history.json")
_WATCHLIST_FILE = os.path.join(_DATA_DIR, "arvio_dashboard_watchlist.json")

# ── Rule-processing dedup ─────────────────────────────────────────────────────
# Prevents triggering Sonarr rule processing more than once per watch session.
# Key: "{tmdb_id}:{season}:{episode}" or "{tmdb_id}:movie"
# Value: unix timestamp of when processing was triggered.
_processed_episodes: Dict[str, float] = {}
_processed_lock = threading.Lock()

_COMPLETION_THRESHOLD_DEFAULT = 85  # fallback if service config is missing
_STATIC_DIR     = os.path.join(os.path.dirname(__file__), "arvio_static")
_LOCK = threading.Lock()

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
            logger.error(f"[Arvio] Failed to load settings: {exc}")
            return None


def _save_settings(data: dict) -> bool:
    with _LOCK:
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            return True
        except Exception as exc:
            logger.error(f"[Arvio] Failed to save settings: {exc}")
            return False


def _get_watchlist_with_status() -> List[dict]:
    """
    Build a watchlist response enriched with Sonarr/Radarr status.
    Reads the settings blob to get watchlist items saved by Arvio,
    then cross-references Sonarr/Radarr to add availability status.
    """
    settings = _load_settings()
    if not settings:
        return []

    raw_items: List[dict] = []

    # Arvio stores watchlist inside the settings blob under each profile.
    # Try the top-level watchlist key first, then fall back to profile-level.
    if "watchlist" in settings:
        raw_items = settings["watchlist"] if isinstance(settings["watchlist"], list) else []

    if not raw_items:
        # Per-profile watchlist: walk profileSettingsById → watchlist
        for profile_settings in (settings.get("profileSettingsById") or {}).values():
            items = profile_settings.get("watchlist") or []
            if isinstance(items, list):
                raw_items.extend(items)

    enriched = []
    for item in raw_items:
        entry = dict(item)
        # Attempt Sonarr/Radarr status enrichment
        try:
            from settings_db import get_sonarr_config, get_radarr_config
            import requests as _requests

            media_type = entry.get("media_type") or entry.get("mediaType") or ""
            tmdb_id = entry.get("tmdb_id") or entry.get("id")

            if media_type in ("tv", "show", "series") and tmdb_id:
                cfg = get_sonarr_config()
                if cfg and cfg.get("url") and cfg.get("api_key"):
                    resp = _requests.get(
                        f"{cfg['url'].rstrip('/')}/api/v3/series",
                        headers={"X-Api-Key": cfg["api_key"]},
                        params={"tmdbId": tmdb_id},
                        timeout=3,
                    )
                    if resp.ok:
                        series_list = resp.json()
                        if series_list:
                            entry["sonarr_status"] = series_list[0].get("status", "unknown")
                            entry["sonarr_id"] = series_list[0].get("id")
            elif media_type == "movie" and tmdb_id:
                cfg = get_radarr_config()
                if cfg and cfg.get("url") and cfg.get("api_key"):
                    resp = _requests.get(
                        f"{cfg['url'].rstrip('/')}/api/v3/movie",
                        headers={"X-Api-Key": cfg["api_key"]},
                        params={"tmdbId": tmdb_id},
                        timeout=3,
                    )
                    if resp.ok:
                        movies = resp.json()
                        if movies:
                            entry["radarr_status"] = "downloaded" if movies[0].get("hasFile") else "monitored"
                            entry["radarr_id"] = movies[0].get("id")
        except Exception as enrich_err:
            logger.debug(f"[Arvio] Watchlist enrichment skipped: {enrich_err}")

        enriched.append(entry)

    return enriched


def _get_completion_threshold() -> float:
    try:
        from settings_db import get_service
        svc = get_service('arvio', 'default')
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
            logger.warning(f"[Arvio] Could not find Sonarr series ID for '{title}' (tmdb={tmdb_id})")
            return

        logger.debug(f"[Arvio] Found Sonarr series_id={series_id} for '{title}'")

        final_rule = None
        try:
            from episeerr import load_config, save_config
            from episeerr_utils import reconcile_series_drift
            config = load_config()
            final_rule, modified = reconcile_series_drift(series_id, config)
            if modified:
                save_config(config)
            logger.debug(f"[Arvio] Rule for series {series_id}: {final_rule}")
        except Exception as drift_err:
            logger.warning(f"[Arvio] Drift reconciliation skipped: {drift_err}")

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
            logger.error(f"[Arvio] media_processor failed (rc={result.returncode}): {result.stderr}")
        else:
            logger.info(f"[Arvio] Rule processing complete for '{title}' S{season}E{episode}")
            if result.stdout:
                logger.debug(f"[Arvio] media_processor stdout: {result.stdout[:500]}")

    except Exception as exc:
        logger.error(f"[Arvio] _trigger_rule_processing error: {exc}", exc_info=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Integration class
# ══════════════════════════════════════════════════════════════════════════════

class ArvioIntegration(ServiceIntegration):

    # ── Metadata ──────────────────────────────────────────────────────────────

    @property
    def service_name(self) -> str:
        return "arvio"

    @property
    def display_name(self) -> str:
        return "Arvio"

    @property
    def description(self) -> str:
        return "Android TV media hub — settings sync, playback events, and watchlist"

    @property
    def icon(self) -> str:
        return ""

    @property
    def category(self) -> str:
        return "utility"

    @property
    def default_port(self) -> int:
        return 7979  # Arvio's LAN watchlist server port

    def get_setup_fields(self):
        return [
            {
                "name":        "progress_threshold",
                "label":       "Progress Threshold (%)",
                "type":        "text",
                "placeholder": "50",
                "required":    False,
                "help_text":   (
                    "Minimum watch percentage before Episeerr triggers next-episode rule processing. "
                    "Match this to the completion % set in the Arvio app (default 50)."
                ),
            },
        ]

    # ── Connection test (not used by Arvio; satisfies base class) ─────────────

    def test_connection(self, url: str, api_key: str) -> Tuple[bool, str]:
        return True, "Arvio connects to Episeerr, not the other way around."

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
            "arvio_integration", __name__,
            url_prefix="/api/integration/arvio",
        )

        # ── GET /status ───────────────────────────────────────────────────────
        @bp.route("/status", methods=["GET"])
        def status():
            """
            Health check — Arvio calls this to verify the server is reachable
            before saving the sync server URL or adding the Episeerr addon.
            """
            settings = _load_settings()
            profiles = len((settings or {}).get("profiles") or []) if settings else 0
            return jsonify({
                "status": "ok",
                "service": "episeerr",
                "arvio_profiles": profiles,
                "settings_present": settings is not None,
            }), 200

        # ── GET /settings ─────────────────────────────────────────────────────
        @bp.route("/settings", methods=["GET"])
        def get_settings():
            """
            Return the full Arvio settings blob.
            Arvio calls this on new-device setup to restore all settings.
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
            Save the full Arvio settings blob.
            Arvio calls this after any settings change or profile operation.
            """
            body = request.get_json(silent=True, force=True)
            if not body or not isinstance(body, dict):
                return jsonify({"error": "Expected a JSON object"}), 400
            ok = _save_settings(body)
            if not ok:
                return jsonify({"error": "Failed to write settings"}), 500
            profiles = len(body.get("profiles") or [])
            logger.info(f"[Arvio] Settings saved — {profiles} profile(s)")
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
                f"[Arvio] {event.upper()} — {title}{ep_info} "
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
                    logger.debug(f"[Arvio] Reset dedup for {ep_key}")

                elif event in ("progress", "stop", "finish"):
                    with _processed_lock:
                        already_processed = ep_key in _processed_episodes

                    if already_processed:
                        logger.debug(f"[Arvio] {ep_key} already processed this session — skipping")
                    else:
                        threshold = _get_completion_threshold()
                        logger.debug(
                            f"[Arvio] {event.upper()} {ep_key} at {progress}% "
                            f"(threshold={threshold}%)"
                        )
                        if progress >= threshold:
                            logger.info(
                                f"[Arvio] Threshold reached for {title}{ep_info} "
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
                                f"[Arvio] Below threshold ({progress}% < {threshold}%) — not processing"
                            )

            return jsonify({"status": "received"}), 200

        # ── GET /watchlist ────────────────────────────────────────────────────
        @bp.route("/watchlist", methods=["GET"])
        def watchlist():
            """
            Return the Arvio watchlist enriched with Sonarr/Radarr status.
            Arvio calls this to display the watchlist row on the home screen.
            """
            items = _get_watchlist_with_status()
            return jsonify(items), 200

        # ── POST /sync ────────────────────────────────────────────────────────
        @bp.route("/sync", methods=["POST"])
        def sync():
            """
            Trigger a watchlist sync: push Arvio watchlist items to
            Sonarr/Radarr for monitoring.
            """
            items = _get_watchlist_with_status()
            synced = 0
            errors = []
            for item in items:
                try:
                    from settings_db import get_sonarr_config, get_radarr_config
                    import requests as _requests

                    media_type = item.get("media_type") or item.get("mediaType") or ""
                    tmdb_id = item.get("tmdb_id") or item.get("id")
                    title = item.get("title", "Unknown")

                    if media_type in ("tv", "show", "series") and tmdb_id and not item.get("sonarr_id"):
                        cfg = get_sonarr_config()
                        if cfg and cfg.get("url") and cfg.get("api_key"):
                            # Look up root folder and quality profile
                            root_resp = _requests.get(
                                f"{cfg['url'].rstrip('/')}/api/v3/rootfolder",
                                headers={"X-Api-Key": cfg["api_key"]}, timeout=3
                            )
                            roots = root_resp.json() if root_resp.ok else []
                            root_path = roots[0]["path"] if roots else "/tv"
                            qp_resp = _requests.get(
                                f"{cfg['url'].rstrip('/')}/api/v3/qualityprofile",
                                headers={"X-Api-Key": cfg["api_key"]}, timeout=3
                            )
                            profiles = qp_resp.json() if qp_resp.ok else []
                            quality_id = profiles[0]["id"] if profiles else 1
                            _requests.post(
                                f"{cfg['url'].rstrip('/')}/api/v3/series",
                                headers={"X-Api-Key": cfg["api_key"]},
                                json={
                                    "tmdbId": tmdb_id, "title": title,
                                    "qualityProfileId": quality_id,
                                    "rootFolderPath": root_path,
                                    "monitored": True, "addOptions": {"searchForMissingEpisodes": False},
                                },
                                timeout=5,
                            )
                            synced += 1
                    elif media_type == "movie" and tmdb_id and not item.get("radarr_id"):
                        cfg = get_radarr_config()
                        if cfg and cfg.get("url") and cfg.get("api_key"):
                            root_resp = _requests.get(
                                f"{cfg['url'].rstrip('/')}/api/v3/rootfolder",
                                headers={"X-Api-Key": cfg["api_key"]}, timeout=3
                            )
                            roots = root_resp.json() if root_resp.ok else []
                            root_path = roots[0]["path"] if roots else "/movies"
                            qp_resp = _requests.get(
                                f"{cfg['url'].rstrip('/')}/api/v3/qualityprofile",
                                headers={"X-Api-Key": cfg["api_key"]}, timeout=3
                            )
                            profiles = qp_resp.json() if qp_resp.ok else []
                            quality_id = profiles[0]["id"] if profiles else 1
                            _requests.post(
                                f"{cfg['url'].rstrip('/')}/api/v3/movie",
                                headers={"X-Api-Key": cfg["api_key"]},
                                json={
                                    "tmdbId": tmdb_id, "title": title,
                                    "qualityProfileId": quality_id,
                                    "rootFolderPath": root_path,
                                    "monitored": True, "addOptions": {"searchForMissingEpisodes": False},
                                },
                                timeout=5,
                            )
                            synced += 1
                except Exception as exc:
                    errors.append(str(exc))

            logger.info(f"[Arvio] Sync complete — {synced} item(s) added, {len(errors)} error(s)")
            return jsonify({"status": "synced", "added": synced, "errors": errors}), 200

        # ── GET/DELETE /history ───────────────────────────────────────────────
        @bp.route("/history", methods=["GET"])
        def get_history():
            limit = int(request.args.get("limit", 200))
            return jsonify(_load_json(_HISTORY_FILE, [])[:limit])

        @bp.route("/history", methods=["DELETE"])
        def clear_history():
            _save_json(_HISTORY_FILE, [])
            return jsonify({"ok": True})

        # ── Dashboard watchlist (separate from TV-app watchlist) ──────────────
        @bp.route("/dashboard/watchlist", methods=["GET"])
        def dashboard_get_watchlist():
            return jsonify(_load_json(_WATCHLIST_FILE, []))

        @bp.route("/dashboard/watchlist", methods=["POST"])
        def dashboard_add_watchlist():
            item = request.get_json(force=True) or {}
            if not item.get("id"):
                return jsonify({"error": "missing id"}), 400
            with _LOCK:
                wl = _load_json(_WATCHLIST_FILE, [])
                exists = any(str(w["id"]) == str(item["id"]) and w.get("mediaType") == item.get("mediaType") for w in wl)
                if not exists:
                    item["inWatchlist"] = True
                    item["addedAt"] = datetime.now(timezone.utc).isoformat()
                    wl.insert(0, item)
                    _save_json(_WATCHLIST_FILE, wl)
            return jsonify({"ok": True})

        @bp.route("/dashboard/watchlist/<media_type>/<item_id>", methods=["DELETE"])
        def dashboard_remove_watchlist(media_type, item_id):
            with _LOCK:
                wl = _load_json(_WATCHLIST_FILE, [])
                wl = [w for w in wl if not (str(w.get("id")) == str(item_id) and w.get("mediaType", "") == media_type)]
                _save_json(_WATCHLIST_FILE, wl)
            return jsonify({"ok": True})

        # ── TMDB search + trending (use Episeerr's configured TMDB key) ────────
        def _tmdb(path, params=None):
            # Prefer Episeerr's configured TMDB key; fall back to key in Arvio settings blob
            key = (current_app.config.get("TMDB_API_KEY") or
                   _load_json(_SETTINGS_FILE, {}).get("tmdb_api_key", ""))
            if not key:
                return None
            p = {"api_key": key}
            if params:
                p.update(params)
            try:
                r = _requests.get(f"https://api.themoviedb.org/3{path}", params=p, timeout=10)
                r.raise_for_status()
                return r.json()
            except Exception:
                return None

        def _map_item(item, media_type=None):
            mt = media_type or item.get("media_type", "movie")
            return {
                "id":          item["id"],
                "title":       item.get("title") or item.get("name", ""),
                "overview":    item.get("overview", ""),
                "image":       "https://image.tmdb.org/t/p/w342" + item["poster_path"] if item.get("poster_path") else "",
                "backdropUrl": "https://image.tmdb.org/t/p/w780" + item["backdrop_path"] if item.get("backdrop_path") else "",
                "mediaType":   "show" if mt == "tv" else "movie",
                "year":        (item.get("release_date") or item.get("first_air_date") or "")[:4],
                "rating":      item.get("vote_average", 0),
                "popularity":  item.get("popularity", 0),
                "inWatchlist": False,
            }

        def _mark_wl(items):
            wl = _load_json(_WATCHLIST_FILE, [])
            keys = {(str(w["id"]), w.get("mediaType", "movie")) for w in wl}
            for item in items:
                item["inWatchlist"] = (str(item["id"]), item.get("mediaType", "movie")) in keys
            return items

        @bp.route("/dashboard/search", methods=["GET"])
        def dashboard_search():
            q = request.args.get("q", "").strip()
            if len(q) < 2:
                return jsonify([])
            data = _tmdb("/search/multi", {"query": q, "page": 1})
            if data is None:
                return jsonify({"error": "TMDB not configured in Episeerr"}), 503
            items = [_map_item(r) for r in data.get("results", [])
                     if r.get("media_type") in ("movie", "tv") and r.get("poster_path")]
            return jsonify(_mark_wl(items))

        @bp.route("/dashboard/trending", methods=["GET"])
        def dashboard_trending():
            movies = _tmdb("/trending/movie/week") or {}
            shows  = _tmdb("/trending/tv/week")    or {}
            items = (
                [_map_item(r, "movie") for r in movies.get("results", []) if r.get("poster_path")]
                + [_map_item(r, "tv")  for r in shows.get("results",  []) if r.get("poster_path")]
            )
            items.sort(key=lambda x: x["popularity"], reverse=True)
            return jsonify(_mark_wl(items[:40]))

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

        # ── UI blueprint (served at /arvio) ───────────────────────────────────
        ui_bp = Blueprint("arvio_ui", __name__, url_prefix="/arvio")

        @ui_bp.route("/", defaults={"path": ""})
        @ui_bp.route("/<path:path>")
        def serve_ui(path):
            if path and os.path.exists(os.path.join(_STATIC_DIR, path)):
                return send_from_directory(_STATIC_DIR, path)
            return send_from_directory(_STATIC_DIR, "index.html")

        return [bp, ui_bp]


# ── Module-level instance (auto-discovered by integrations/__init__.py) ───────

integration = ArvioIntegration()

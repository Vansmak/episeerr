"""
Arvio Integration for Episeerr
───────────────────────────────
Provides webhook reception, watchlist enrichment, and sync triggers.
The full setup portal lives at the standalone arvio-server.

Sync API (prefix: /api/integration/arvio):
    GET  /status      ← Arvio pings this to verify Episeerr is reachable
    POST /webhook     ← Arvio posts playback events (start/pause/stop/progress)
    GET  /watchlist   ← Arvio fetches its watchlist enriched with Sonarr/Radarr status
    POST /sync        ← Arvio triggers a watchlist sync to Sonarr/Radarr
"""

import os
import json
import logging
import threading
import requests as _requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request
from integrations.base import ServiceIntegration

logger = logging.getLogger(__name__)

# ── File paths ────────────────────────────────────────────────────────────────

_DATA_DIR      = os.path.join(os.getcwd(), "data")
_SETTINGS_FILE = os.path.join(_DATA_DIR, "arvio_settings.json")
_LOCK = threading.Lock()


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

    if "watchlist" in settings:
        raw_items = settings["watchlist"] if isinstance(settings["watchlist"], list) else []

    if not raw_items:
        for profile_settings in (settings.get("profileSettingsById") or {}).values():
            items = profile_settings.get("watchlist") or []
            if isinstance(items, list):
                raw_items.extend(items)

    enriched = []
    for item in raw_items:
        entry = dict(item)
        try:
            from settings_db import get_sonarr_config, get_radarr_config

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


# ══════════════════════════════════════════════════════════════════════════════
#  Integration class
# ══════════════════════════════════════════════════════════════════════════════

class ArvioIntegration(ServiceIntegration):

    @property
    def service_name(self) -> str:
        return "arvio"

    @property
    def display_name(self) -> str:
        return "Arvio"

    @property
    def description(self) -> str:
        return "Android TV media hub — playback webhooks and watchlist enrichment"

    @property
    def icon(self) -> str:
        return ""

    @property
    def category(self) -> str:
        return "utility"

    @property
    def default_port(self) -> int:
        return 7979

    def get_setup_fields(self):
        return []

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

    def create_blueprint(self) -> Blueprint:
        bp = Blueprint(
            "arvio_integration", __name__,
            url_prefix="/api/integration/arvio",
        )

        # ── GET /status ───────────────────────────────────────────────────────
        @bp.route("/status", methods=["GET"])
        def status():
            settings = _load_settings()
            profiles = len((settings or {}).get("profiles") or []) if settings else 0
            return jsonify({
                "status": "ok",
                "service": "episeerr",
                "arvio_profiles": profiles,
                "settings_present": settings is not None,
            }), 200

        # ── POST /webhook ─────────────────────────────────────────────────────
        @bp.route("/webhook", methods=["POST"])
        def webhook():
            data = request.get_json(silent=True) or {}
            event      = data.get("event", "unknown")
            title      = data.get("title", "Unknown")
            tmdb_id    = data.get("tmdb_id")
            media_type = data.get("media_type", "?")
            progress   = data.get("progress_percent", 0)
            season     = data.get("season")
            episode    = data.get("episode")

            ep_info = f" S{season:02d}E{episode:02d}" if season and episode else ""
            logger.info(
                f"[Arvio] {event.upper()} — {title}{ep_info} "
                f"({media_type} tmdb={tmdb_id}) {progress}%"
            )

            return jsonify({"status": "received"}), 200

        # ── GET /watchlist ────────────────────────────────────────────────────
        @bp.route("/watchlist", methods=["GET"])
        def watchlist():
            items = _get_watchlist_with_status()
            return jsonify(items), 200

        # ── POST /sync ────────────────────────────────────────────────────────
        @bp.route("/sync", methods=["POST"])
        def sync():
            items = _get_watchlist_with_status()
            synced = 0
            errors = []
            for item in items:
                try:
                    from settings_db import get_sonarr_config, get_radarr_config

                    media_type = item.get("media_type") or item.get("mediaType") or ""
                    tmdb_id = item.get("tmdb_id") or item.get("id")
                    title = item.get("title", "Unknown")

                    if media_type in ("tv", "show", "series") and tmdb_id and not item.get("sonarr_id"):
                        cfg = get_sonarr_config()
                        if cfg and cfg.get("url") and cfg.get("api_key"):
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

        return bp


# ── Module-level instance (auto-discovered by integrations/__init__.py) ───────

integration = ArvioIntegration()

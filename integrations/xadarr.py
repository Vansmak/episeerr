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

Generic notifications (top-level /api, not nested under /api/integration/xadarr —
must match {sync_server}/api/notify/recent, which is what NotificationPollManager
on the TV app actually polls):
    POST /api/notify         ← generic notification push (title/message/type/source)
    GET  /api/notify/recent  ← polled by NotificationPollManager for on-device toasts

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
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request, send_from_directory, Response, stream_with_context, redirect, render_template
from integrations.base import ServiceIntegration
from episeerr_utils import http as _http

logger = logging.getLogger(__name__)

# ── File paths ────────────────────────────────────────────────────────────────

_DATA_DIR      = os.path.join(os.getcwd(), "data")
_SETTINGS_FILE = os.path.join(_DATA_DIR, "xadarr_settings.json")
_HISTORY_FILE  = os.path.join(_DATA_DIR, "xadarr_history.json")
_TRAKT_OUTBOX_FILE = os.path.join(_DATA_DIR, "xadarr_trakt_outbox.json")

# ── Rule-processing dedup ─────────────────────────────────────────────────────
# Prevents triggering Sonarr rule processing more than once per watch session.
# Key: "{tmdb_id}:{season}:{episode}" or "{tmdb_id}:movie"
# Value: unix timestamp of when processing was triggered.
#
# File-backed, not a plain dict (2026-07-13 fix) — same reason _NOTIFICATIONS_FILE
# is file-backed (see comment near _store_notification): Episeerr runs under
# gunicorn with multiple worker processes, each its own Python memory. The
# progress webhook fires every ~30s during playback; whichever worker happens to
# handle each request has no way to know another worker already marked an
# episode processed, so every worker that sees a post-threshold progress ping
# independently re-triggered rule processing and re-fired the "rule.triggered"
# toast for the same episode all watch-session long (Joe: "watching Masters of
# the Air and getting way too many toasts about s1e2").
_PROCESSED_EPISODES_FILE = os.path.join(_DATA_DIR, "xadarr_processed_episodes.json")
_processed_lock = threading.Lock()


def _processed_episodes_load() -> Dict[str, float]:
    return _load_json(_PROCESSED_EPISODES_FILE, {})


def _processed_episodes_save(data: Dict[str, float]) -> None:
    # Prune entries older than 24h so this file doesn't grow forever — dedup
    # only needs to survive a single watch session, not accumulate across days.
    cutoff = time.time() - 86400
    pruned = {k: v for k, v in data.items() if v >= cutoff}
    _save_json(_PROCESSED_EPISODES_FILE, pruned)

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


def _xw_trakt_refresh_token(blob: dict) -> Optional[str]:
    """Refresh this dashboard's own Trakt connection (traktTokens/trakt_client_id
    in the blob — separate from integrations/trakt.py's own connection) and
    persist the new tokens into the given (already-loaded) blob. Returns the
    new access token, or None if there's nothing to refresh with or the
    refresh call itself fails.
    """
    tokens = (blob.get("traktTokens") or {}).get(_XW_PROFILE, {})
    refresh_token = tokens.get("refreshToken")
    client_id = blob.get("trakt_client_id", "")
    client_secret = blob.get("trakt_client_secret", "")
    if not refresh_token or not client_id or not client_secret:
        return None
    try:
        r = _http.post(
            "https://api.trakt.tv/oauth/token",
            json={
                "refresh_token": refresh_token, "client_id": client_id,
                "client_secret": client_secret, "grant_type": "refresh_token",
            },
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        tok = r.json()
        new_tokens = {
            "accessToken": tok.get("access_token"),
            "refreshToken": tok.get("refresh_token"),
            "expiresAt": int(time.time() * 1000) + tok.get("expires_in", 0) * 1000,
        }
        blob.setdefault("traktTokens", {})[_XW_PROFILE] = new_tokens
        return new_tokens["accessToken"]
    except Exception as exc:
        logger.warning(f"[Xadarr] Trakt token refresh failed: {exc}")
        return None


def _xw_trakt_request(blob: dict, method: str, path: str, json_body: dict = None):
    """Authenticated call against this dashboard's own Trakt connection
    (traktTokens/trakt_client_id in the blob — separate from
    integrations/trakt.py's own connection), refreshing reactively on a 401
    rather than trusting the stored expiresAt (observed holding a stale value
    in the wrong units — seconds vs. ms — for tokens saved by an older build;
    proactively refreshing on every call hits Trakt's aggressive refresh-
    endpoint rate limit fast). Raises on any failure, including "not
    connected" — callers decide whether to swallow or propagate.
    """
    tokens = (blob.get("traktTokens") or {}).get(_XW_PROFILE, {})
    access_token = tokens.get("accessToken")
    if not access_token:
        raise RuntimeError("Trakt not connected")
    client_id = blob.get("trakt_client_id", "")

    def _call(token: str):
        headers = {
            "Authorization": f"Bearer {token}",
            "trakt-api-key": client_id,
            "trakt-api-version": "2",
            "Content-Type": "application/json",
        }
        if method == "GET":
            return _http.get(f"https://api.trakt.tv/{path}", headers=headers, timeout=10)
        return _http.post(f"https://api.trakt.tv/{path}", json=json_body, headers=headers, timeout=10)

    r = _call(access_token)
    if r.status_code == 401:
        refreshed = _xw_trakt_refresh_token(blob)
        if refreshed:
            r = _call(refreshed)
    r.raise_for_status()
    return r


def _xw_trakt_outbox_load() -> list:
    return _load_json(_TRAKT_OUTBOX_FILE, [])


def _xw_trakt_outbox_enqueue(tmdb_id: int, media_type: str, add: bool) -> None:
    items = _xw_trakt_outbox_load()
    items.append({
        "tmdbId": tmdb_id, "mediaType": media_type, "add": add,
        "queuedAt": int(time.time() * 1000),
    })
    _save_json(_TRAKT_OUTBOX_FILE, items)


def _xw_flush_trakt_outbox(blob: dict) -> None:
    """Retry any watchlist pushes that failed at the time of the original
    add/remove. Runs opportunistically on every watchlist GET (mirrors the
    native app's own outbox, which flushes on its periodic/reconnect Trakt
    sync) — makes this dashboard's Trakt push as reliable as the app's, which
    matters once Trakt is treated as the cross-surface source of truth: a
    push that's silently lost here is indistinguishable from a real removal
    once anything reconciles against Trakt.
    """
    items = _xw_trakt_outbox_load()
    if not items:
        return
    remaining = []
    for item in items:
        try:
            key = "movies" if item.get("mediaType") == "movie" else "shows"
            body = {key: [{"ids": {"tmdb": item.get("tmdbId")}}]}
            path = "sync/watchlist" if item.get("add") else "sync/watchlist/remove"
            _xw_trakt_request(blob, "POST", path, body)
        except Exception as exc:
            logger.warning(f"[Xadarr] Trakt outbox retry failed for {item}: {exc}")
            remaining.append(item)
    if len(remaining) != len(items):
        _save_json(_TRAKT_OUTBOX_FILE, remaining)


def _xw_trakt_watchlist_push(blob: dict, tmdb_id: int, media_type: str, add: bool) -> None:
    """Push a watchlist add/remove to Trakt. Never raises — a Trakt failure
    here must not block the local watchlist write, same as the native Xadarr
    app's toggleWatchlist()/TraktOutboxRepository. Unlike a plain best-effort
    attempt, a failure here is queued (see _xw_trakt_outbox_enqueue) and
    retried on the next watchlist load instead of being silently dropped.
    """
    key = "movies" if media_type == "movie" else "shows"
    body = {key: [{"ids": {"tmdb": tmdb_id}}]}
    path = "sync/watchlist" if add else "sync/watchlist/remove"
    try:
        _xw_trakt_request(blob, "POST", path, body)
    except Exception as exc:
        logger.warning(
            f"[Xadarr] Trakt watchlist {'add' if add else 'remove'} failed for tmdb={tmdb_id}: {exc}"
        )
        _xw_trakt_outbox_enqueue(tmdb_id, media_type, add)


def _xw_reconcile_watchlist_with_trakt(blob: dict) -> list:
    """Make watchlistByProfile match Trakt's real watchlist exactly — Trakt is
    the single source of truth, this blob is a cache. Adds anything Trakt has
    that's missing locally (fetching title/poster from TMDB), drops anything
    local that Trakt no longer has. Never raises; if Trakt can't be reached
    (not connected, network error) returns the local list untouched rather
    than risk wiping it out over a transient failure.

    Safe to run unconditionally now that _xw_flush_trakt_outbox is called
    first on the same request — a push that failed at add/remove time gets
    one more chance to reach Trakt before this treats "not on Trakt" as
    ground truth. (Previously reverted 2026-07-11: reconciling against Trakt
    while pushes could be silently and permanently lost resurrected watchlist
    items the user had deliberately removed.)
    """
    try:
        r = _xw_trakt_request(blob, "GET", "sync/watchlist?extended=full")
        trakt_items = r.json()
    except Exception as exc:
        logger.warning(f"[Xadarr] Trakt watchlist fetch failed, skipping reconcile: {exc}")
        return _xw_watchlist(blob)

    trakt_keys = set()
    for entry in trakt_items:
        media_type = "movie" if entry.get("type") == "movie" else "show"
        obj = entry.get("movie") or entry.get("show") or {}
        tmdb_id = (obj.get("ids") or {}).get("tmdb")
        if tmdb_id:
            trakt_keys.add((media_type, tmdb_id))

    local = _xw_watchlist(blob)
    kept = [
        w for w in local
        if (_xw_norm_media(w.get("mediaType", "movie")), int(w.get("tmdbId") or w.get("id") or 0)) in trakt_keys
    ]
    local_keys = {
        (_xw_norm_media(w.get("mediaType", "movie")), int(w.get("tmdbId") or w.get("id") or 0))
        for w in local
    }
    missing = trakt_keys - local_keys
    changed = len(kept) != len(local)

    for media_type, tmdb_id in missing:
        endpoint = "/movie/" if media_type == "movie" else "/tv/"
        tmdb_data = _xw_tmdb(endpoint + str(tmdb_id)) or {}
        kept.insert(0, {
            "tmdbId": tmdb_id,
            "title": tmdb_data.get("title") or tmdb_data.get("name") or "",
            "mediaType": media_type,
            "posterPath": ("https://image.tmdb.org/t/p/w342" + tmdb_data["poster_path"])
                if tmdb_data.get("poster_path") else "",
            "backdropPath": ("https://image.tmdb.org/t/p/w780" + tmdb_data["backdrop_path"])
                if tmdb_data.get("backdrop_path") else "",
            "addedAt": int(time.time() * 1000),
            "sourceOrder": 0,
        })
        changed = True

    if changed:
        _xw_set_watchlist(blob, kept)
        _xw_save_blob(blob)
    return kept


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


NEOLINK_RECORDINGS_DIR = Path(os.environ.get("NEOLINK_RECORDINGS_DIR", "/neolink-recordings"))

_neolink_token_cache = {"token": None, "fetched_at": 0.0}

# Neolink has no live-snapshot API (only event thumbnails), so camera grid tiles
# would otherwise show a stale frame from whenever the last motion event fired.
# Grab a real current frame off Neolink's RTSP restream via ffmpeg instead, cached
# briefly per camera so rapid repeat requests (grid re-render, polling) don't each
# spawn their own ffmpeg process.
_SNAPSHOT_CACHE_DIR = Path(tempfile.gettempdir()) / "xadarr_camera_snapshots"
_SNAPSHOT_TTL_SECONDS = 15


def _neolink_live_snapshot(camera_name: str, cfg: dict) -> Optional[bytes]:
    host = urlparse(cfg["url"]).hostname if cfg.get("url") else None
    if not host:
        return None
    _SNAPSHOT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _SNAPSHOT_CACHE_DIR / f"{camera_name}.jpg"
    try:
        if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < _SNAPSHOT_TTL_SECONDS:
            return cache_path.read_bytes()
    except Exception:
        pass
    rtsp_url = f"rtsp://{host}:8654/{camera_name}/subStream"
    tmp_path = cache_path.with_suffix(".jpg.tmp")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", rtsp_url,
             "-frames:v", "1", "-q:v", "4", "-f", "mjpeg", str(tmp_path)],
            capture_output=True, timeout=6,
        )
        if result.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
            tmp_path.replace(cache_path)
            return cache_path.read_bytes()
    except Exception as e:
        logger.warning(f"[neolink] live snapshot grab failed for {camera_name}: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)
    # Stale cache beats no image at all.
    try:
        return cache_path.read_bytes() if cache_path.exists() else None
    except Exception:
        return None


def _get_neolink_config(blob: dict = None) -> dict:
    if blob is None:
        blob = _load_json(_SETTINGS_FILE, {})
    return {
        "url": blob.get("neolink_url", "").rstrip("/"),
        "username": blob.get("neolink_username", ""),
        "password": blob.get("neolink_password", ""),
    }


def _neolink_login(cfg: dict) -> Optional[str]:
    try:
        r = _http.post(
            f"{cfg['url']}/api/auth/login",
            json={"username": cfg["username"], "password": cfg["password"]},
            timeout=8,
        )
        r.raise_for_status()
        token = r.json().get("token")
        if token:
            _neolink_token_cache["token"] = token
            _neolink_token_cache["fetched_at"] = time.time()
        return token
    except Exception as e:
        logger.warning(f"[neolink] login failed: {e}")
        return None


def _neolink_token(cfg: dict, force_refresh: bool = False) -> Optional[str]:
    stale = (time.time() - _neolink_token_cache["fetched_at"]) > 3600
    if force_refresh or not _neolink_token_cache["token"] or stale:
        return _neolink_login(cfg)
    return _neolink_token_cache["token"]


def _neolink_get(cfg: dict, path: str, timeout: int = 8):
    """GET against Neolink's API, re-logging in once on a 401."""
    token = _neolink_token(cfg)
    if not token:
        return None
    r = _http.get(f"{cfg['url']}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    if r.status_code == 401:
        token = _neolink_token(cfg, force_refresh=True)
        if not token:
            return None
        r = _http.get(f"{cfg['url']}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    r.raise_for_status()
    return r


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
    """POST an event to xadarr-server's inbound webhook in a background thread.

    Always stores locally first so the on-device toast poll sees the event even
    when no separate xadarr-server is configured (the common case for Episeerr
    users, who sync directly against this Episeerr instance)."""
    notify_xadarr_event(event, payload)

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


# ── Generic notification queue ────────────────────────────────────────────────
# Mirrors xadarr-server's /api/notify implementation so on-device toast polling
# (NotificationPollManager, 60s poll of {sync_server}/api/notify/recent) works
# the same way whether a device's configured sync server is Episeerr or
# xadarr-server. Needed for users who don't run Episeerr at all — this endpoint
# is backend-agnostic, not Episeerr-specific.
#
# File-backed, not an in-memory list: Episeerr runs under gunicorn with
# multiple worker processes (each its own Python memory), so a POST landing on
# one worker and a poll GET landing on another would see nothing with a plain
# in-process deque — confirmed empty on first deploy. A shared JSON file (same
# pattern as _HISTORY_FILE/_WEBHOOK_LOG_FILE elsewhere in this module) is
# visible to every worker.

_NOTIFICATIONS_FILE = os.path.join(_DATA_DIR, "xadarr_notifications.json")

_XADARR_EVENT_TYPE_MAP = {
    "episode.grabbed": "grab",
    "episode.ready": "ready",
    "rule.triggered": "info",
    "rule.assigned": "info",
    "watchlist.requested": "info",
    "channel.failover": "warning",
}


def _make_notification(source: str, title: str, message: Optional[str], notif_type: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": source,
        "title": title,
        "message": message or "",
        "type": notif_type,
    }


def _store_notification(entry: dict) -> None:
    with _LOCK:
        notifications = _load_json(_NOTIFICATIONS_FILE, [])
        notifications.insert(0, entry)
        _save_json(_NOTIFICATIONS_FILE, notifications[:100])
    payload = "event: notification\ndata: " + json.dumps(entry) + "\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)


_NOTIFY_DEDUP_FILE = os.path.join(_DATA_DIR, "xadarr_notify_dedup.json")
_notify_dedup_lock = threading.Lock()
_NOTIFY_DEDUP_COOLDOWN = 1800  # 30 min
_NOTIFY_DEDUP_EVENTS = {"episode.grabbed", "episode.ready"}


def _notify_dedup_should_fire(event: str, key: str) -> bool:
    # rule.triggered already has its own per-session dedup (_PROCESSED_EPISODES_FILE
    # above). grabbed/ready toasts had none — confirmed 2026-07-13/14 that Sonarr
    # re-fires its Grab webhook for the SAME episode many times in a short burst
    # when a release keeps failing to import (Masters of the Air, then Lioness
    # S1E2: 6 grabs in ~8 minutes, Joe: "too many toast messages"). Collapse
    # repeats within a cooldown window into one toast rather than suppressing
    # forever — a genuinely still-stuck download should resurface occasionally,
    # not vanish.
    if event not in _NOTIFY_DEDUP_EVENTS:
        return True
    with _notify_dedup_lock:
        data = _load_json(_NOTIFY_DEDUP_FILE, {})
        now = time.time()
        data = {k: v for k, v in data.items() if now - v < 86400}
        last = data.get(key)
        if last is not None and now - last < _NOTIFY_DEDUP_COOLDOWN:
            _save_json(_NOTIFY_DEDUP_FILE, data)
            return False
        data[key] = now
        _save_json(_NOTIFY_DEDUP_FILE, data)
        return True


def notify_xadarr_event(event: str, payload: dict) -> None:
    """Store an Episeerr-originated event (grab/ready/rule/watchlist/failover)
    into the generic notification queue so it shows as an on-device toast —
    independent of fire_xadarr_webhook, which only fires when a separate
    xadarr-server instance is configured under Services."""
    title = str(payload.get("title") or "Unknown")
    season = payload.get("season")
    episode = payload.get("episode")

    if not _notify_dedup_should_fire(event, f"{event}:{title}:{season}:{episode}"):
        return

    message = payload.get("rule") or (
        f"S{season:02d}E{episode:02d}" if season and episode else None
    )
    entry = _make_notification(
        source="Episeerr",
        title=title,
        message=message,
        notif_type=_XADARR_EVENT_TYPE_MAP.get(event, "info"),
    )
    _store_notification(entry)


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


def _default_home_layout() -> dict:
    """
    Default home_layout.hero/footer for a profile with nothing stored yet.
    Mirrors HomeLayoutRepository on the TV app and xadarr-server's
    _default_home_layout() — same "homeLayoutByProfile" blob key so a change
    on either sync backend round-trips identically. Rows/nav already have
    homes (catalogsByProfile / navSectionsByProfile) — this is only for the
    two zones that didn't.
    """
    return {
        "hero": {"type": "live_resume", "actions": ["watch", "guide"]},
        "footer": {"type": "apps_catalog"},
    }


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
            fire_xadarr_webhook("rule.triggered", {
                "title": title,
                "season": season,
                "episode": episode,
                "rule": final_rule,
            })

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
            # One-time backfill: a profile with no stored homeLayoutByProfile
            # entry gets the default persisted, matching xadarr-server. Not
            # load-bearing for the TV app (it defaults locally too) — just
            # keeps the on-disk blob complete for direct inspection.
            profiles = data.get("profiles") or []
            if profiles:
                by_profile = data.setdefault("homeLayoutByProfile", {})
                changed = False
                for profile in profiles:
                    pid = profile.get("id")
                    if pid and pid not in by_profile:
                        by_profile[pid] = _default_home_layout()
                        changed = True
                if changed:
                    _save_settings(data)
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
            # Preserve traktTokens per profile if the incoming blob omits a
            # profile's token. Trakt refresh tokens are single-use — whichever
            # device refreshes first rotates it, and any other device still
            # holding the old refresh token gets a 401 on its next refresh
            # attempt and clears its own local copy. Without this guard, that
            # device's very next settings push (for any unrelated change)
            # would wipe the still-valid, just-rotated token this server
            # already has, forcing every device to re-authenticate. Only a
            # profile the client explicitly disconnected (traktDisconnectedProfileIds)
            # is allowed to clear the stored token.
            existing_trakt_tokens = existing.get("traktTokens") or {}
            if existing_trakt_tokens:
                incoming_trakt_tokens = body.setdefault("traktTokens", {})
                disconnected_ids = set(body.get("traktDisconnectedProfileIds") or [])
                for pid, existing_token in existing_trakt_tokens.items():
                    if not isinstance(existing_token, dict) or not existing_token.get("accessToken"):
                        continue
                    if pid in disconnected_ids:
                        continue
                    incoming_token = incoming_trakt_tokens.get(pid)
                    if not isinstance(incoming_token, dict) or not incoming_token.get("accessToken"):
                        incoming_trakt_tokens[pid] = existing_token
            # Preserve neolink_username/neolink_password if the incoming blob omits them.
            # The TV app's Neolink URL field is a single string with no separate
            # username/password inputs — it only round-trips credentials it can
            # decompose out of that field, which are never set locally. Without this,
            # every settings push from a device wipes the credentials entered via the
            # web dashboard's separate URL/Username/Password rows within seconds.
            if not (body.get("neolink_username") or "").strip():
                existing_user = (existing.get("neolink_username") or "").strip()
                if existing_user:
                    body["neolink_username"] = existing_user
            if not (body.get("neolink_password") or "").strip():
                existing_pass = (existing.get("neolink_password") or "").strip()
                if existing_pass:
                    body["neolink_password"] = existing_pass
            # Preserve homeLayoutByProfile per profile if the incoming blob omits
            # it — an old (pre-home_layout) client's PUT is a full overwrite and
            # would otherwise silently wipe hero/footer config set from another device.
            existing_home_layout = existing.get("homeLayoutByProfile") or {}
            if existing_home_layout:
                incoming_home_layout = body.setdefault("homeLayoutByProfile", {})
                for pid, layout in existing_home_layout.items():
                    incoming_home_layout.setdefault(pid, layout)
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
                        data = _processed_episodes_load()
                        if data.pop(ep_key, None) is not None:
                            _processed_episodes_save(data)
                    logger.debug(f"[Xadarr] Reset dedup for {ep_key}")

                elif event in ("progress", "stop", "finish"):
                    threshold = _get_completion_threshold()
                    logger.debug(
                        f"[Xadarr] {event.upper()} {ep_key} at {progress}% "
                        f"(threshold={threshold}%)"
                    )
                    if progress >= threshold:
                        # Check-and-mark atomically under the lock, *before* triggering —
                        # not after — so a second worker landing here (even moments
                        # later, while _trigger_rule_processing's subprocess call is
                        # still running) sees the mark and skips instead of racing in.
                        with _processed_lock:
                            data = _processed_episodes_load()
                            already_processed = ep_key in data
                            if not already_processed:
                                data[ep_key] = time.time()
                                _processed_episodes_save(data)

                        if already_processed:
                            logger.debug(f"[Xadarr] {ep_key} already processed this session — skipping")
                        else:
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
                    else:
                        logger.debug(
                            f"[Xadarr] Below threshold ({progress}% < {threshold}%) — not processing"
                        )

            return jsonify({"status": "received"}), 200

        # ── Compat: old /api/integration/arvio/* URLs ────────────────────────
        arvio_bp = Blueprint("arvio_compat", __name__, url_prefix="/api/integration/arvio")

        @arvio_bp.route("/webhook", methods=["POST"])
        def arvio_webhook_compat():
            return webhook()

        @arvio_bp.route("/status", methods=["GET"])
        def arvio_status_compat():
            return status()

        @arvio_bp.route("/settings", methods=["GET", "PUT"])
        def arvio_settings_compat():
            return settings() if request.method == "GET" else save_settings()

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

        @ui_bp.route("/webhook", methods=["POST"])
        def xadarr_short_webhook():
            return webhook()

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
                msg = str(exc)
                if "401" in msg:
                    return jsonify({"error": "Authentication failed — wrong username or password"}), 400
                return jsonify({"error": msg}), 500

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
            _xw_flush_trakt_outbox(blob)
            items = _xw_reconcile_watchlist_with_trakt(blob)
            return jsonify(_xw_watchlist_to_web(items))

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
                _xw_trakt_watchlist_push(blob, tmdb_id, media_type, add=True)
                _xw_save_blob(blob)
            return jsonify({"ok": True})

        @web_bp.route("/media/watchlist/<media_type>/<int:item_id>", methods=["DELETE"])
        def web_delete_watchlist(media_type, item_id):
            blob = _xw_blob()
            wl = [w for w in _xw_watchlist(blob) if int(w.get("tmdbId") or w.get("id") or 0) != item_id]
            _xw_set_watchlist(blob, wl)
            _xw_trakt_watchlist_push(blob, item_id, _xw_norm_media(media_type), add=False)
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

        # ── Cameras (Neolink) ────────────────────────────────────────────────
        @web_bp.route("/cameras/list", methods=["GET"])
        def web_cameras_list():
            blob = _xw_blob()
            cfg = _get_neolink_config(blob)
            if not cfg["url"]:
                return jsonify([])
            try:
                r = _neolink_get(cfg, "/api/cameras")
                if r is None:
                    return jsonify({"error": "neolink auth failed"}), 502
                cameras = [
                    {"name": cam["name"], "snapshotUrl": f"/xadarr/api/cameras/snapshot/{cam['name']}"}
                    for cam in r.json()
                ]
                return jsonify(cameras)
            except Exception as exc:
                return jsonify({"error": str(exc)}), 502

        @web_bp.route("/cameras/snapshot/<camera_name>", methods=["GET"])
        def web_camera_snapshot(camera_name):
            cfg = _get_neolink_config(_xw_blob())
            live = _neolink_live_snapshot(camera_name, cfg) if cfg["url"] else None
            if live:
                resp = Response(live, mimetype="image/jpeg")
                resp.headers["Cache-Control"] = "no-cache"
                return resp
            # Live RTSP grab failed (camera offline, ffmpeg missing, etc.) — fall
            # back to the most recent event's thumbnail (folders are named so
            # lexical sort == chronological).
            cam_dir = NEOLINK_RECORDINGS_DIR / camera_name
            if not cam_dir.is_dir():
                return "No recordings for camera", 404
            try:
                for date_dir in sorted((d for d in cam_dir.iterdir() if d.is_dir()), reverse=True):
                    detections_dir = date_dir / "detections"
                    if not detections_dir.is_dir():
                        continue
                    for event_dir in sorted((d for d in detections_dir.iterdir() if d.is_dir()), reverse=True):
                        if (event_dir / "thumb.jpg").exists():
                            resp = send_from_directory(str(event_dir), "thumb.jpg", mimetype="image/jpeg")
                            resp.headers["Cache-Control"] = "no-cache"
                            return resp
                return "No thumbnail available", 404
            except Exception as exc:
                return str(exc), 502

        @web_bp.route("/cameras/ws-token", methods=["GET"])
        def web_cameras_ws_token():
            blob = _xw_blob()
            cfg = _get_neolink_config(blob)
            if not cfg["url"]:
                return jsonify({"error": "neolink not configured"}), 503
            token = _neolink_token(cfg)
            if not token:
                return jsonify({"error": "neolink auth failed"}), 502
            ws_base = cfg["url"].replace("https://", "wss://").replace("http://", "ws://")
            return jsonify({"token": token, "wsBase": ws_base})

        def _serve_neolink_event_asset(event_id: str, filename: str, mimetype: str):
            # event ids look like "camera~YYYY-MM-DD~HHMMSS-hash", matching the
            # on-disk layout recordings/<camera>/<date>/detections/<HHMMSS-hash>/
            parts = event_id.split("~")
            if len(parts) != 3:
                return "Bad event id", 400
            camera, date, event_folder = parts
            event_dir = NEOLINK_RECORDINGS_DIR / camera / date / "detections" / event_folder
            if not (event_dir / filename).exists():
                return "Not found", 404
            return send_from_directory(str(event_dir), filename, mimetype=mimetype)

        @web_bp.route("/cameras/events/<event_id>/thumb.jpg", methods=["GET"])
        def web_camera_event_thumb(event_id):
            return _serve_neolink_event_asset(event_id, "thumb.jpg", "image/jpeg")

        @web_bp.route("/cameras/events/<event_id>/clip.mp4", methods=["GET"])
        def web_camera_event_clip(event_id):
            return _serve_neolink_event_asset(event_id, "clip.mp4", "video/mp4")

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

        # ── Sonarr proxy helpers ──────────────────────────────────────────────
        def _sonarr_series_status():
            """Return episode statuses for a series season via Sonarr API."""
            import requests as _req
            from settings_db import get_sonarr_config
            tvdb_id = request.args.get("tvdbId", "")
            season  = request.args.get("season",  "")
            if not tvdb_id or not season:
                return jsonify({"error": "tvdbId and season are required"}), 400
            try:
                season_num = int(season)
            except ValueError:
                return jsonify({"error": "season must be an integer"}), 400
            cfg = get_sonarr_config()
            sonarr_url = (cfg.get("url") or "").rstrip("/")
            api_key    = cfg.get("api_key") or ""
            if not sonarr_url or not api_key:
                return jsonify({"error": "Sonarr not configured"}), 503
            headers = {"X-Api-Key": api_key}
            try:
                # Resolve TVDB ID → Sonarr series ID
                r = _req.get(f"{sonarr_url}/api/v3/series?tvdbId={tvdb_id}", headers=headers, timeout=10)
                r.raise_for_status()
                series_list = r.json()
                if not series_list:
                    return jsonify({"episodes": {}})
                series_id = series_list[0]["id"]

                # Fetch episodes for this season
                r = _req.get(
                    f"{sonarr_url}/api/v3/episode?seriesId={series_id}&seasonNumber={season_num}",
                    headers=headers, timeout=10
                )
                r.raise_for_status()
                episodes = r.json()

                # Fetch queue to find downloading episodes
                r = _req.get(f"{sonarr_url}/api/v3/queue?seriesId={series_id}&pageSize=100", headers=headers, timeout=10)
                r.raise_for_status()
                queue_records = r.json().get("records", [])
                queued = {}  # episode_id → progress %
                for rec in queue_records:
                    ep_id = rec.get("episodeId")
                    if ep_id is None:
                        continue
                    size     = rec.get("size", 0) or 0
                    left     = rec.get("sizeleft", size) or size
                    progress = round((1 - left / size) * 100, 1) if size > 0 else 0.0
                    queued[ep_id] = progress

                result = {}
                for ep in episodes:
                    ep_num  = ep.get("episodeNumber")
                    ep_id   = ep.get("id")
                    has_file    = ep.get("hasFile", False)
                    monitored   = ep.get("monitored", False)
                    air_date    = ep.get("airDateUtc") or ep.get("airDate") or ""
                    from datetime import datetime, timezone
                    aired = False
                    if air_date:
                        try:
                            aired = datetime.fromisoformat(air_date.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
                        except Exception:
                            aired = True
                    if ep_id in queued:
                        status = "queued"
                        progress = queued[ep_id]
                    elif has_file:
                        status = "available"
                        progress = 0.0
                    elif aired:
                        # "missing" regardless of Sonarr's own `monitored` flag —
                        # Episeerr's rules often only monitor the next N episodes at
                        # a time, but a manual search here is a deliberate user
                        # override, not automatic RSS/indexer monitoring, so an
                        # aired-but-unmonitored episode should still be searchable
                        # instead of silently showing no badge at all.
                        status = "missing"
                        progress = 0.0
                    elif monitored:
                        status = "monitored"
                        progress = 0.0
                    else:
                        status = "unmonitored"
                        progress = 0.0
                    result[str(ep_num)] = {"status": status, "progress": progress}
                return jsonify({"episodes": result})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        def _sonarr_episode_search():
            """Trigger a Sonarr episode search by TVDB ID + season + episode number."""
            import requests as _req
            from settings_db import get_sonarr_config
            data     = request.get_json(silent=True) or {}
            tvdb_id  = str(data.get("tvdbId", ""))
            season   = data.get("season")
            episode  = data.get("episode")
            if not tvdb_id or season is None or episode is None:
                return jsonify({"success": False, "error": "tvdbId, season, episode are required"}), 400
            cfg = get_sonarr_config()
            sonarr_url = (cfg.get("url") or "").rstrip("/")
            api_key    = cfg.get("api_key") or ""
            if not sonarr_url or not api_key:
                return jsonify({"success": False, "error": "Sonarr not configured"}), 503
            headers = {"X-Api-Key": api_key}
            try:
                r = _req.get(f"{sonarr_url}/api/v3/series?tvdbId={tvdb_id}", headers=headers, timeout=10)
                r.raise_for_status()
                series_list = r.json()
                if not series_list:
                    return jsonify({"success": False, "error": "Series not found in Sonarr"})
                series_id = series_list[0]["id"]
                r = _req.get(
                    f"{sonarr_url}/api/v3/episode?seriesId={series_id}&seasonNumber={season}",
                    headers=headers, timeout=10
                )
                r.raise_for_status()
                eps = [e for e in r.json() if e.get("episodeNumber") == episode]
                if not eps:
                    return jsonify({"success": False, "error": "Episode not found"})
                ep_id = eps[0]["id"]
                # Manually searching an episode Sonarr isn't currently monitoring
                # (Episeerr's rules often only monitor the next N episodes at a time)
                # still needs monitored=True, or Sonarr's import decision-making can
                # skip a release it otherwise finds — this is a deliberate user
                # override of that monitoring state, not a bypass of it.
                if not eps[0].get("monitored", False):
                    _req.put(
                        f"{sonarr_url}/api/v3/episode/monitor",
                        json={"episodeIds": [ep_id], "monitored": True},
                        headers=headers, timeout=10
                    )
                r = _req.post(
                    f"{sonarr_url}/api/v3/command",
                    json={"name": "EpisodeSearch", "episodeIds": [ep_id]},
                    headers=headers, timeout=10
                )
                r.raise_for_status()
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 500

        def _sonarr_episode_delete():
            """Delete one episode's file by TVDB ID + season + episode number —
            couch-triggered "remove this episode" from the Xadarr Details screen,
            same resolution steps as _sonarr_episode_search."""
            import requests as _req
            from settings_db import get_sonarr_config
            data     = request.get_json(silent=True) or {}
            tvdb_id  = str(data.get("tvdbId", ""))
            season   = data.get("season")
            episode  = data.get("episode")
            if not tvdb_id or season is None or episode is None:
                return jsonify({"success": False, "error": "tvdbId, season, episode are required"}), 400
            cfg = get_sonarr_config()
            sonarr_url = (cfg.get("url") or "").rstrip("/")
            api_key    = cfg.get("api_key") or ""
            if not sonarr_url or not api_key:
                return jsonify({"success": False, "error": "Sonarr not configured"}), 503
            headers = {"X-Api-Key": api_key}
            try:
                r = _req.get(f"{sonarr_url}/api/v3/series?tvdbId={tvdb_id}", headers=headers, timeout=10)
                r.raise_for_status()
                series_list = r.json()
                if not series_list:
                    return jsonify({"success": False, "error": "Series not found in Sonarr"})
                series_id = series_list[0]["id"]
                r = _req.get(
                    f"{sonarr_url}/api/v3/episode?seriesId={series_id}&seasonNumber={season}",
                    headers=headers, timeout=10
                )
                r.raise_for_status()
                eps = [e for e in r.json() if e.get("episodeNumber") == episode]
                if not eps:
                    return jsonify({"success": False, "error": "Episode not found"})
                episode_file_id = eps[0].get("episodeFileId")
                if not episode_file_id:
                    return jsonify({"success": False, "error": "Episode has no file"}), 400
                r = _req.delete(
                    f"{sonarr_url}/api/v3/episodeFile/{episode_file_id}",
                    headers=headers, timeout=15
                )
                if not r.ok:
                    return jsonify({"success": False, "error": f"Sonarr delete failed: {r.status_code}"}), 500
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 500

        def _sonarr_calendar():
            """Upcoming episodes across all monitored series, for the Xadarr Upcoming row."""
            import requests as _req
            from datetime import datetime, timedelta, timezone
            from settings_db import get_sonarr_config
            days = int(request.args.get("days", "180"))
            cfg = get_sonarr_config()
            sonarr_url = (cfg.get("url") or "").rstrip("/")
            api_key = cfg.get("api_key") or ""
            if not sonarr_url or not api_key:
                return jsonify({"error": "Sonarr not configured"}), 503
            headers = {"X-Api-Key": api_key}
            start = datetime.now(timezone.utc).date().isoformat()
            end = (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()
            try:
                # Default (monitored-only) calendar: a show with messy/partial
                # monitoring state (some episodes downloaded out of order, most
                # unmonitored) should not surface a stale "next" episode just because
                # unmonitored=true widened the query. Sonarr's own monitored flag on
                # the episode is the simplest, most honest signal for "this is really
                # coming up next" for a show Joe is tracking.
                r = _req.get(
                    f"{sonarr_url}/api/v3/calendar?start={start}&end={end}&includeSeries=true",
                    headers=headers, timeout=15,
                )
                r.raise_for_status()
                entries = []
                for ep in r.json():
                    series = ep.get("series") or {}
                    if not series.get("monitored", False):
                        continue
                    if ep.get("hasFile", False):
                        continue
                    season_num = ep.get("seasonNumber", 0)
                    if season_num == 0:
                        continue
                    series_id = series.get("id")
                    # Sonarr's own cached cover - matches what Sonarr's UI shows for this
                    # series. More reliable than the `images[].remoteUrl` TheTVDB gives
                    # back in the calendar payload, which has been seen returning
                    # mismatched artwork for some series.
                    poster = (
                        f"{sonarr_url}/api/v3/mediacover/{series_id}/poster.jpg?apikey={api_key}"
                        if series_id else None
                    )
                    entries.append({
                        "seriesId": series_id,
                        "tvdbId": series.get("tvdbId"),
                        "title": series.get("title", ""),
                        "season": season_num,
                        "episode": ep.get("episodeNumber", 0),
                        "episodeTitle": ep.get("title", ""),
                        "airDate": ep.get("airDateUtc") or ep.get("airDate") or "",
                        "poster": poster,
                    })
                return jsonify({"episodes": entries})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        # ── /api/episeerr/* aliases ───────────────────────────────────────────
        # The TV app routes Episeerr calls through $SYNC_SERVER_URL/api/episeerr/*.
        # xadarr-server proxies those to Episeerr's real endpoints; when Episeerr is
        # the sync server (Joe's setup) the app hits Episeerr directly at these paths.
        # Forward to the same Flask view functions the main app registers.
        alias_bp = Blueprint("xadarr_api_alias", __name__, url_prefix="/api")

        # The TV app builds camera/snapshot/clip URLs against $SYNC_SERVER_URL/api/cameras/*
        # directly (matching xadarr-server's own top-level route convention, not web_bp's
        # /xadarr/api/* prefix) — alias them the same way as the /episeerr/* routes above.
        @alias_bp.route("/cameras/list", methods=["GET"])
        def alias_cameras_list():
            return web_cameras_list()

        @alias_bp.route("/cameras/snapshot/<camera_name>", methods=["GET"])
        def alias_camera_snapshot(camera_name):
            return web_camera_snapshot(camera_name)

        @alias_bp.route("/cameras/events/<event_id>/thumb.jpg", methods=["GET"])
        def alias_camera_event_thumb(event_id):
            return web_camera_event_thumb(event_id)

        @alias_bp.route("/cameras/events/<event_id>/clip.mp4", methods=["GET"])
        def alias_camera_event_clip(event_id):
            return web_camera_event_clip(event_id)

        @alias_bp.route("/cameras/ws-token", methods=["GET"])
        def alias_cameras_ws_token():
            return web_cameras_ws_token()

        @alias_bp.route("/episeerr/pending", methods=["GET"])
        def alias_episeerr_pending():
            return get_pending()

        @alias_bp.route("/episeerr/rules", methods=["GET"])
        def alias_episeerr_rules():
            import flask
            fn = flask.current_app.view_functions.get("api_rules_list")
            if fn:
                return fn()
            # Fallback: build the expected {"rules": [...]} format inline
            try:
                from episeerr import load_config
                config = load_config()
                rules = config.get("rules", {})
                rules_list = [
                    {
                        "name": k,
                        "display_name": k.replace("_", " ").title(),
                        "series_count": len(v.get("series", {})),
                    }
                    for k, v in sorted(rules.items())
                ]
                return jsonify({"rules": rules_list})
            except Exception:
                return jsonify({"rules": []})

        @alias_bp.route("/episeerr/assign", methods=["POST"])
        def alias_episeerr_assign():
            import flask
            fn = flask.current_app.view_functions.get("api_assign_pending_rule")
            if fn:
                return fn()
            return jsonify({"success": False, "error": "assign endpoint unavailable"}), 503

        @alias_bp.route("/episeerr/assign-series", methods=["POST"])
        def alias_episeerr_assign_series():
            """Direct rule assignment for an already-tracked series (library
            browser), bypassing the pending-request requirement of /assign."""
            import flask
            fn = flask.current_app.view_functions.get("api_assign_series_rule")
            if fn:
                return fn()
            return jsonify({"success": False, "error": "assign-series endpoint unavailable"}), 503

        @alias_bp.route("/sonarr/series-status", methods=["GET"])
        def alias_sonarr_series_status():
            return _sonarr_series_status()

        @alias_bp.route("/sonarr/episode-search", methods=["POST"])
        def alias_sonarr_episode_search():
            return _sonarr_episode_search()

        @alias_bp.route("/sonarr/episode-delete", methods=["POST"])
        def alias_sonarr_episode_delete():
            return _sonarr_episode_delete()

        @alias_bp.route("/sonarr/calendar", methods=["GET"])
        def alias_sonarr_calendar():
            return _sonarr_calendar()

        # ── Generic notifications (polled by NotificationPollManager at
        # {sync_server}/api/notify/recent — a top-level path, not nested under
        # /api/integration/xadarr, so these belong on alias_bp not bp) ─────────
        @alias_bp.route("/notify", methods=["POST"])
        def post_notify():
            data = request.get_json(force=True, silent=True) or {}
            title = str(data.get("title") or "").strip()
            if not title:
                return jsonify({"ok": False, "error": "title required"}), 400
            entry = _make_notification(
                source=str(data.get("source") or "Unknown").strip(),
                title=title,
                message=str(data.get("message") or "").strip() or None,
                notif_type=str(data.get("type") or "info").strip(),
            )
            _store_notification(entry)
            return jsonify({"ok": True, "id": entry["id"]})

        @alias_bp.route("/notify/recent", methods=["GET"])
        def get_notify_recent():
            limit = min(int(request.args.get("limit", 20)), 100)
            since = request.args.get("since", "")
            items = _load_json(_NOTIFICATIONS_FILE, [])
            if since:
                items = [n for n in items if n.get("timestamp", "") > since]
            return jsonify(items[:limit])

        return [bp, ui_bp, addon_bp, dc_bp, web_bp, arvio_bp, alias_bp]


# ── Module-level instance (auto-discovered by integrations/__init__.py) ───────

integration = XadarrIntegration()

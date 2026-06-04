"""
Movie cleanup processor for Episeerr.
Handles Radarr movie rules: tag management, watch history, and cleanup scheduling.
Completely separate from series rules - no shared config, no episode/season concepts.
"""
import os
import re
import time
import json
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from episeerr_utils import normalize_url, http
from logging_config import main_logger as logger
from media_processor import (
    setup_cleanup_logging, load_config, load_global_settings, parse_date_fixed
)

load_dotenv()

cleanup_logger = setup_cleanup_logging()

# ─── Radarr config ───────────────────────────────────────────────────────────

def get_radarr_settings():
    from settings_db import get_radarr_config
    config = get_radarr_config()
    if config:
        return normalize_url(config.get('url')), config.get('api_key')
    return normalize_url(os.getenv('RADARR_URL')), os.getenv('RADARR_API_KEY')


def get_tautulli_settings():
    from settings_db import get_service
    config = get_service('tautulli', 'default')
    if config:
        return normalize_url(config.get('url')), config.get('api_key')
    return normalize_url(os.getenv('TAUTULLI_URL')), os.getenv('TAUTULLI_API_KEY')


def get_plex_settings():
    from settings_db import get_service
    config = get_service('plex', 'default')
    if config:
        return normalize_url(config.get('url')), config.get('api_key')
    return normalize_url(os.getenv('PLEX_URL')), os.getenv('PLEX_TOKEN')


def get_jellyfin_settings():
    from settings_db import get_service
    config = get_service('jellyfin', 'default')
    if config:
        return normalize_url(config.get('url')), config.get('api_key'), config.get('user_id')
    return normalize_url(os.getenv('JELLYFIN_URL')), os.getenv('JELLYFIN_API_KEY'), os.getenv('JELLYFIN_USER_ID')


def get_emby_settings():
    from settings_db import get_service
    config = get_service('emby', 'default')
    if config:
        return normalize_url(config.get('url')), config.get('api_key'), config.get('user_id')
    return normalize_url(os.getenv('EMBY_URL')), os.getenv('EMBY_API_KEY'), os.getenv('EMBY_USER_ID')


# ─── Tag management ──────────────────────────────────────────────────────────

def _rule_to_tag_label(rule_name):
    """Convert a movie rule name to a Radarr tag label.
    'Watched Movies' → 'episeerr-watched-movies'
    Radarr tag labels must match ^[a-z0-9-]+ (no underscores).
    """
    slug = re.sub(r'[^a-z0-9]+', '-', rule_name.lower()).strip('-')
    return f"episeerr-{slug}"


def get_or_create_radarr_tag(rule_name, radarr_url, api_key):
    """Create episeerr_<slug> tag in Radarr if it doesn't already exist.
    Returns tag ID or None on failure.
    """
    tag_label = _rule_to_tag_label(rule_name)
    headers = {'X-Api-Key': api_key}
    try:
        resp = http.get(f"{radarr_url}/api/v3/tag", headers=headers, timeout=10)
        if not resp.ok:
            logger.error(f"Radarr tag list failed: {resp.status_code}")
            return None
        tags = resp.json()
        for tag in tags:
            if tag['label'].lower() == tag_label.lower():
                return tag['id']

        # Create
        create_resp = http.post(
            f"{radarr_url}/api/v3/tag",
            headers=headers,
            json={"label": tag_label},
            timeout=10
        )
        if create_resp.ok:
            tag_id = create_resp.json().get('id')
            logger.info(f"Created Radarr tag '{tag_label}' (ID {tag_id})")
            return tag_id
        else:
            # Tag may have been created concurrently (second gunicorn worker, race condition)
            retry = http.get(f"{radarr_url}/api/v3/tag", headers=headers, timeout=10)
            if retry.ok:
                for tag in retry.json():
                    if tag['label'].lower() == tag_label.lower():
                        logger.info(f"Radarr tag '{tag_label}' already exists (ID {tag['id']})")
                        return tag['id']
            logger.error(f"Failed to create Radarr tag '{tag_label}': {create_resp.text[:300]}")
            return None
    except Exception as e:
        logger.error(f"Error managing Radarr tag '{tag_label}': {e}")
        return None


def ensure_movie_rule_tags(movie_rules):
    """Create Radarr tags for all movie rules. Called on startup/save."""
    radarr_url, api_key = get_radarr_settings()
    if not radarr_url or not api_key:
        return {}
    tag_ids = {}
    for rule_name in movie_rules:
        tag_id = get_or_create_radarr_tag(rule_name, radarr_url, api_key)
        if tag_id:
            tag_ids[rule_name] = tag_id
    return tag_ids


# ─── Watch history ────────────────────────────────────────────────────────────

def _norm_title(t):
    t = t.lower()
    t = re.sub(r'\s*\(\d{4}\)', '', t)
    t = re.sub(r'[^\w\s]', ' ', t)
    return ' '.join(t.split())


def _build_plex_watch_cache():
    """Build {tmdb_id_str: unix_ts} from all Plex movie libraries."""
    url, token = get_plex_settings()
    if not url or not token:
        return {}
    headers = {'Accept': 'application/json', 'X-Plex-Token': token}
    cache = {}
    try:
        sections_resp = http.get(f"{url}/library/sections", headers=headers, timeout=10)
        if not sections_resp.ok:
            return {}
        sections = sections_resp.json().get('MediaContainer', {}).get('Directory', [])
        movie_sections = [s['key'] for s in sections if s.get('type') == 'movie']
        for section_key in movie_sections:
            items_resp = http.get(
                f"{url}/library/sections/{section_key}/all",
                params={'type': 1, 'includeGuids': 1},
                headers=headers,
                timeout=30
            )
            if not items_resp.ok:
                continue
            for item in items_resp.json().get('MediaContainer', {}).get('Metadata', []):
                last_viewed = item.get('lastViewedAt')
                if not last_viewed:
                    continue
                for guid in item.get('Guid', []):
                    gid = guid.get('id', '')
                    if gid.startswith('tmdb://'):
                        tmdb_id = gid[7:]
                        if int(last_viewed) > cache.get(tmdb_id, 0):
                            cache[tmdb_id] = int(last_viewed)
    except Exception as e:
        logger.warning(f"Plex watch cache error: {e}")
    return cache


def _build_jellyfin_emby_watch_cache(is_emby=False):
    """Build {tmdb_id_str: unix_ts} from Jellyfin or Emby."""
    server_name = 'Emby' if is_emby else 'Jellyfin'
    url, api_key, user_id = get_emby_settings() if is_emby else get_jellyfin_settings()
    if not url or not api_key or not user_id:
        return {}
    headers = {'X-Emby-Token': api_key}
    cache = {}
    try:
        resp = http.get(
            f"{url}/Users/{user_id}/Items",
            params={'IncludeItemTypes': 'Movie', 'Recursive': 'true', 'Fields': 'UserData,ProviderIds', 'Limit': 10000},
            headers=headers,
            timeout=30
        )
        if not resp.ok:
            return {}
        for item in resp.json().get('Items', []):
            last_played = item.get('UserData', {}).get('LastPlayedDate')
            if not last_played:
                continue
            tmdb_id = str(item.get('ProviderIds', {}).get('Tmdb', ''))
            if not tmdb_id:
                continue
            try:
                ts = int(datetime.fromisoformat(last_played.rstrip('Z')).replace(tzinfo=timezone.utc).timestamp())
                if ts > cache.get(tmdb_id, 0):
                    cache[tmdb_id] = ts
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"{server_name} watch cache error: {e}")
    return cache


def _build_tautulli_watch_cache():
    """Build {norm_title: unix_ts} from full Tautulli movie history."""
    tautulli_url, api_key = get_tautulli_settings()
    if not tautulli_url or not api_key:
        return {}
    cache = {}
    try:
        resp = http.get(
            f"{tautulli_url}/api/v2",
            params={'apikey': api_key, 'cmd': 'get_history', 'media_type': 'movie', 'length': 10000, 'start': 0},
            timeout=30
        )
        if not resp.ok:
            return {}
        data = resp.json()
        if data.get('response', {}).get('result') != 'success':
            return {}
        for entry in data.get('response', {}).get('data', {}).get('data', []):
            title = entry.get('title', '')
            ts = entry.get('date')
            if not title or not ts:
                continue
            norm = _norm_title(title)
            if int(ts) > cache.get(norm, 0):
                cache[norm] = int(ts)
    except Exception as e:
        logger.warning(f"Tautulli watch cache error: {e}")
    return cache


MOVIE_WATCH_EVENTS_FILE = os.path.join(os.getcwd(), 'data', 'movie_watch_events.json')


def record_movie_watched(tmdb_id: str, title: str, user: str = 'Unknown') -> None:
    """Record a real-time webhook movie watch event to persistent cache.

    Called by Jellyfin/Emby/Plex/Tautulli webhook handlers on movie PlaybackStop
    or scrobble. build_movie_watch_cache() reads this file so the cleanup
    scheduler picks up the event without waiting for the next full library scan.
    """
    ts = int(time.time())
    try:
        os.makedirs(os.path.dirname(MOVIE_WATCH_EVENTS_FILE), exist_ok=True)
        events: dict = {}
        if os.path.exists(MOVIE_WATCH_EVENTS_FILE):
            with open(MOVIE_WATCH_EVENTS_FILE, 'r') as f:
                events = json.load(f)
        if int(events.get(str(tmdb_id), 0)) < ts:
            events[str(tmdb_id)] = ts
        with open(MOVIE_WATCH_EVENTS_FILE, 'w') as f:
            json.dump(events, f)
        logger.info(f"[movie] Recorded watch: '{title}' (tmdb={tmdb_id}) by {user}")
    except Exception as e:
        logger.warning(f"[movie] record_movie_watched error: {e}")


def _build_webhook_watch_cache() -> dict:
    """Load real-time webhook-recorded watch events from data/movie_watch_events.json."""
    if not os.path.exists(MOVIE_WATCH_EVENTS_FILE):
        return {}
    try:
        with open(MOVIE_WATCH_EVENTS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[movie] webhook watch cache load error: {e}")
        return {}


def build_movie_watch_cache():
    """
    Build unified watch cache. Returns (tmdb_cache, title_cache, sources).
    Priority: Webhook events → Plex → Jellyfin → Emby → Tautulli (title fallback).
    Webhook events come first so real-time watch detection beats polling.
    """
    tmdb_cache = {}
    title_cache = {}
    sources = []

    for build_fn, label in [
        (_build_plex_watch_cache, 'Plex'),
        (lambda: _build_jellyfin_emby_watch_cache(False), 'Jellyfin'),
        (lambda: _build_jellyfin_emby_watch_cache(True), 'Emby'),
    ]:
        part = build_fn()
        if part:
            for k, v in part.items():
                if v > tmdb_cache.get(k, 0):
                    tmdb_cache[k] = v
            sources.append(label)

    title_cache = _build_tautulli_watch_cache()
    if title_cache:
        sources.append('Tautulli')

    # Merge webhook-recorded events last — they always win (most recent timestamp)
    webhook_cache = _build_webhook_watch_cache()
    if webhook_cache:
        for k, v in webhook_cache.items():
            if int(v) > tmdb_cache.get(k, 0):
                tmdb_cache[k] = int(v)
        sources.append('Webhook')

    logger.info(
        f"Movie watch cache: {', '.join(sources) or 'none'} — "
        f"{len(tmdb_cache)} TMDB entries, {len(title_cache)} title entries"
    )
    return tmdb_cache, title_cache, sources


# ─── Deletion helpers ─────────────────────────────────────────────────────────

def delete_movie(movie, radarr_url, api_key, delete_option):
    """Execute deletion.
    delete_option: 'file_only' → DELETE /api/v3/moviefile/{movieFileId}
                   'remove_from_radarr' → DELETE /api/v3/movie/{id}?deleteFiles=true
    """
    headers = {'X-Api-Key': api_key}
    try:
        if delete_option == 'remove_from_radarr':
            url = f"{radarr_url}/api/v3/movie/{movie['id']}?deleteFiles=true"
            resp = http.delete(url, headers=headers, timeout=15)
        else:
            movie_file_id = movie.get('movieFile', {}).get('id')
            if not movie_file_id:
                cleanup_logger.warning(f"No movieFile.id for '{movie['title']}' — cannot delete file")
                return False
            url = f"{radarr_url}/api/v3/moviefile/{movie_file_id}"
            resp = http.delete(url, headers=headers, timeout=15)

        if resp.ok:
            cleanup_logger.info(f"✅ Deleted movie '{movie['title']}' ({delete_option})")
            return True
        else:
            cleanup_logger.error(f"❌ Failed to delete '{movie['title']}': {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        cleanup_logger.error(f"Error deleting movie '{movie['title']}': {e}")
        return False


# ─── Main cleanup ─────────────────────────────────────────────────────────────

def run_movie_cleanup():
    """
    Movie cleanup pass — mirrors the series grace cleanup pattern.
    Called from run_unified_cleanup() in media_processor.py after series phases.
    """
    try:
        config = load_config()
        movie_rules = config.get('movie_rules', {})

        if not movie_rules:
            cleanup_logger.info("🎬 No movie rules configured — skipping movie cleanup")
            return 0

        radarr_url, api_key = get_radarr_settings()
        if not radarr_url or not api_key:
            cleanup_logger.warning("🎬 Radarr not configured — skipping movie cleanup")
            return 0

        global_settings = load_global_settings()
        global_dry_run = global_settings.get('dry_run_mode', False)

        cleanup_logger.info("🎬 MOVIE CLEANUP: Checking Radarr movies")
        if global_dry_run:
            cleanup_logger.info("🛡️ Global dry run — movie deletions queued for approval")

        headers = {'X-Api-Key': api_key}

        # Fetch all movies
        movies_resp = http.get(f"{radarr_url}/api/v3/movie", headers=headers, timeout=30)
        if not movies_resp.ok:
            cleanup_logger.error(f"Failed to fetch movies from Radarr: {movies_resp.status_code}")
            return 0
        all_movies = movies_resp.json()

        # Build tag map: id → label
        tags_resp = http.get(f"{radarr_url}/api/v3/tag", headers=headers, timeout=10)
        all_tags = tags_resp.json() if tags_resp.ok else []
        tag_map = {t['id']: t['label'] for t in all_tags}

        # Build reverse map: tag_label_slug → rule_name
        slug_to_rule = {_rule_to_tag_label(rn): rn for rn in movie_rules}

        current_time = int(time.time())
        total_flagged = 0

        # Build watch cache once for all movies
        tmdb_watch_cache, title_watch_cache, cache_sources = build_movie_watch_cache()
        cleanup_logger.info(f"🎬 Watch sources: {', '.join(cache_sources) or 'none'}")

        for movie in all_movies:
            try:
                movie_id = movie['id']
                movie_title = movie.get('title', f'movie_{movie_id}')

                # Only process movies with an episeerr- tag
                movie_tag_labels = [tag_map.get(tid, '') for tid in movie.get('tags', [])]
                matching_tags = [lbl for lbl in movie_tag_labels if lbl.startswith('episeerr-')]
                if not matching_tags:
                    continue

                # Find the first matching rule
                rule_name = None
                for tag_lbl in matching_tags:
                    if tag_lbl in slug_to_rule:
                        rule_name = slug_to_rule[tag_lbl]
                        break

                if not rule_name:
                    cleanup_logger.warning(
                        f"🎬 '{movie_title}' has tag(s) {matching_tags} but no matching movie rule"
                    )
                    continue

                rule = movie_rules[rule_name]

                # Skip if no file on disk
                if not movie.get('hasFile'):
                    continue

                grace_watched = rule.get('grace_watched')
                dormant_days = rule.get('dormant_days') or rule.get('grace_unwatched')

                if not grace_watched and not dormant_days:
                    continue

                # Look up watch history from cache (TMDB ID first, title fallback)
                tmdb_id = str(movie.get('tmdbId', ''))
                last_watched_ts = None
                watch_source = None

                if tmdb_id and tmdb_id in tmdb_watch_cache:
                    last_watched_ts = tmdb_watch_cache[tmdb_id]
                    watch_source = 'media server'
                else:
                    norm = _norm_title(movie_title)
                    if norm in title_watch_cache:
                        last_watched_ts = title_watch_cache[norm]
                        watch_source = 'Tautulli'

                flagged = False
                flag_reason = None
                date_source = "Unknown"
                date_value = "N/A"

                if last_watched_ts:
                    days_since = (current_time - last_watched_ts) / 86400
                    if grace_watched and days_since > grace_watched:
                        flagged = True
                        flag_reason = f"Grace Watched ({grace_watched}d — last watched {days_since:.1f}d ago)"
                        date_source = watch_source or 'Watch History'
                        date_value = datetime.fromtimestamp(last_watched_ts).strftime('%Y-%m-%d')
                else:
                    # dormant_days: never watched — use Radarr added date
                    if dormant_days:
                        added_str = movie.get('added')
                        added_ts = parse_date_fixed(added_str, f"movie {movie_id}") if added_str else None
                        if added_ts:
                            days_since = (current_time - added_ts) / 86400
                            if days_since > dormant_days:
                                flagged = True
                                flag_reason = f"Dormant ({dormant_days}d — added {days_since:.1f}d ago, never watched)"
                                date_source = "Radarr"
                                date_value = datetime.fromtimestamp(added_ts).strftime('%Y-%m-%d')

                if not flagged:
                    continue

                rule_dry_run = rule.get('dry_run', False)
                is_dry_run = global_dry_run or rule_dry_run
                require_approval = rule.get('require_approval', True)
                delete_option = rule.get('delete_option', 'file_only')

                if is_dry_run or require_approval:
                    from pending_deletions import queue_movie_deletion
                    movie_file = movie.get('movieFile', {})
                    file_size = movie_file.get('size', 0)
                    movie_file_id = movie_file.get('id')

                    if movie_file_id:
                        queue_movie_deletion(
                            movie_id=movie_id,
                            movie_title=movie_title,
                            movie_file_id=movie_file_id,
                            file_size=file_size,
                            rule_name=rule_name,
                            reason=flag_reason,
                            date_source=date_source,
                            date_value=date_value,
                            delete_option=delete_option,
                        )
                        mode = "DRY RUN" if is_dry_run else "PENDING APPROVAL"
                        cleanup_logger.info(f"🎬 [{mode}] '{movie_title}': {flag_reason}")
                    else:
                        cleanup_logger.warning(f"🎬 '{movie_title}' flagged but has no movieFile.id — skipping queue")
                else:
                    success = delete_movie(movie, radarr_url, api_key, delete_option)
                    if success:
                        cleanup_logger.info(f"🎬 Deleted '{movie_title}': {flag_reason}")

                total_flagged += 1

            except Exception as e:
                cleanup_logger.error(f"Error processing movie {movie.get('title', movie_id)}: {e}", exc_info=True)

        cleanup_logger.info(f"🎬 Movie cleanup: {total_flagged} movie(s) flagged")
        return total_flagged

    except Exception as e:
        cleanup_logger.error(f"Error in movie cleanup: {e}", exc_info=True)
        return 0

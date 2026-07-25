"""
Pending Deletions Management System - v3.0.0
Handles queuing, approval, and rejection of episode and movie deletions.

Changes in v3.0.0:
- Added movie pending deletions (queue_movie_deletion, approve_movie_deletions, etc.)
- File format migrated from bare list to {"episodes": [...], "movies": [...]}

Changes in v2.9.0:
- Added queue_deletion() wrapper for simpler API
- Batched deletions by series for efficiency (one delete command per series instead of per episode)
"""
import os
import json
import logging
from datetime import datetime, timedelta
from threading import Lock
from collections import defaultdict

logger = logging.getLogger(__name__)

# File paths
PENDING_DELETIONS_FILE = os.path.join(os.getcwd(), 'data', 'pending_deletions.json')
REJECTION_CACHE_FILE = os.path.join(os.getcwd(), 'data', 'deletion_rejections.json')
MOVIE_REJECTION_CACHE_FILE = os.path.join(os.getcwd(), 'data', 'movie_deletion_rejections.json')
os.makedirs(os.path.dirname(PENDING_DELETIONS_FILE), exist_ok=True)

# Thread safety
pending_lock = Lock()
rejection_lock = Lock()
movie_rejection_lock = Lock()

# Rejection cache duration (days)
REJECTION_CACHE_DAYS = 30


def _load_raw():
    """Load raw file content, migrate list→dict format if needed."""
    try:
        if os.path.exists(PENDING_DELETIONS_FILE):
            with open(PENDING_DELETIONS_FILE, 'r') as f:
                raw = json.load(f)
            # Migrate old bare-list format
            if isinstance(raw, list):
                return {"episodes": raw, "movies": []}
            return raw
    except Exception as e:
        logger.error(f"Error loading pending deletions: {e}")
    return {"episodes": [], "movies": []}


def _save_raw(data):
    try:
        with open(PENDING_DELETIONS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving pending deletions: {e}")


def load_pending_deletions():
    """Load episode pending deletions list (backwards-compatible)."""
    return _load_raw().get("episodes", [])


def save_pending_deletions(pending_list):
    """Save episode pending deletions list (preserves movies section)."""
    data = _load_raw()
    data["episodes"] = pending_list
    _save_raw(data)


def load_rejection_cache():
    """Load rejection cache from file"""
    try:
        if os.path.exists(REJECTION_CACHE_FILE):
            with open(REJECTION_CACHE_FILE, 'r') as f:
                cache = json.load(f)
                # Clean expired entries
                return cleanup_expired_rejections(cache)
        return {}
    except Exception as e:
        logger.error(f"Error loading rejection cache: {e}")
        return {}


def save_rejection_cache(cache):
    """Save rejection cache to file"""
    try:
        with open(REJECTION_CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving rejection cache: {e}")


def cleanup_expired_rejections(cache):
    """Remove expired rejections from cache"""
    today = datetime.now().strftime('%Y-%m-%d')
    expired = [eid for eid, exp_date in cache.items() if exp_date < today]
    for eid in expired:
        del cache[eid]
    return cache


def is_episode_rejected(episode_id):
    """Check if an episode is in the rejection cache"""
    with rejection_lock:
        cache = load_rejection_cache()
        return str(episode_id) in cache


def queue_deletion(series_id, series_title, season_number, episode_number, episode_id, 
                   episode_file_id, episode_title, file_size, 
                   reason, date_source, date_value, rule_name):
    """
    NEW v2.9.0: Simplified wrapper for queueing deletions
    
    Args:
        series_id: Sonarr series ID
        series_title: Name of the series
        season_number: Season number
        episode_number: Episode number
        episode_id: Sonarr episode ID
        episode_file_id: Sonarr episode file ID
        episode_title: Episode title
        file_size: File size in bytes
        reason: Why this episode is marked for deletion
        date_source: Where the date came from
        date_value: The actual date used for decision
        rule_name: Name of the rule that triggered this
    """
    # Build minimal episode object
    episode = {
        'id': episode_id,
        'seriesId': series_id,
        'seasonNumber': season_number,
        'episodeNumber': episode_number,
        'title': episode_title,
        'series': {'title': series_title},
        'episodeFile': {
            'id': episode_file_id,
            'size': file_size
        }
    }
    
    # Use existing add function
    add_to_pending_deletions(episode, reason, date_source, date_value, rule_name)


def add_to_pending_deletions(episode, reason, date_source, date_value, rule_name):
    """
    Add an episode to pending deletions queue
    
    Args:
        episode: Episode data from Sonarr (or minimal object from queue_deletion)
        reason: Why this episode is marked for deletion (e.g., "Grace Period", "Keep Rule")
        date_source: Where the date came from ("Tautulli" or "Sonarr Air Date")
        date_value: The actual date used for decision
        rule_name: Name of the rule that triggered this
    """
    # Skip if in rejection cache
    if is_episode_rejected(episode['id']):
        logger.debug(f"Skipping episode {episode['id']} - in rejection cache")
        return
    
    with pending_lock:
        pending_list = load_pending_deletions()
        
        series_id = episode['seriesId']
        season_num = episode['seasonNumber']
        episode_id = episode['id']
        
        # Find or create series entry
        series_entry = next((s for s in pending_list if s['series_id'] == series_id), None)
        if not series_entry:
            series_entry = {
                'series_id': series_id,
                'series_title': episode['series']['title'],
                'seasons': {}
            }
            pending_list.append(series_entry)
        
        # Find or create season entry
        season_key = str(season_num)
        if season_key not in series_entry['seasons']:
            series_entry['seasons'][season_key] = {
                'season_number': season_num,
                'episodes': []
            }
        
        # Check if episode already exists
        existing_episode = next(
            (ep for ep in series_entry['seasons'][season_key]['episodes'] if ep['episode_id'] == episode_id),
            None
        )
        
        if existing_episode:
            logger.debug(f"Episode {episode_id} already in pending deletions")
            return
        
        # Add episode
        series_entry['seasons'][season_key]['episodes'].append({
            'episode_id': episode_id,
            'episode_number': episode['episodeNumber'],
            'title': episode.get('title', 'Unknown'),
            'reason': reason,
            'rule_name': rule_name,
            'date_source': date_source,
            'date_value': date_value,
            'file_size_mb': round(episode.get('episodeFile', {}).get('size', 0) / (1024 * 1024), 2),
            'queued_at': datetime.now().isoformat(),
            'episode_data': episode  # Store full episode for actual deletion
        })
        
        save_pending_deletions(pending_list)
        logger.info(f"Added to pending deletions: {episode['series']['title']} S{season_num:02d}E{episode['episodeNumber']:02d} - {reason}")


def get_pending_deletions_summary():
    """Get summary of pending deletions for display"""
    with pending_lock:
        pending_list = load_pending_deletions()
        
        total_episodes = 0
        total_size_mb = 0
        
        for series in pending_list:
            for season_data in series['seasons'].values():
                total_episodes += len(season_data['episodes'])
                total_size_mb += sum(ep['file_size_mb'] for ep in season_data['episodes'])
        
        return {
            'total_series': len(pending_list),
            'total_episodes': total_episodes,
            'total_size_mb': total_size_mb,
            'total_size_gb': round(total_size_mb / 1024, 2),
            'pending_list': pending_list
        }


def approve_deletions(episode_ids, sonarr_delete_func):
    """
    NEW v2.9.0: Approve and execute deletions with BATCHED DELETIONS BY SERIES
    
    Args:
        episode_ids: List of episode IDs to delete
        sonarr_delete_func: Function to call to actually delete episodes
    
    Returns:
        dict with success count and any errors
    """
    # Only hold pending_lock while touching the on-disk queue itself. The delete
    # calls below can recurse back into add_to_pending_deletions() (e.g. if a
    # deletion turns out to still be dry-run for some other reason) which also
    # takes pending_lock — that lock is a plain, non-reentrant Lock, so calling
    # sonarr_delete_func() while still holding it would deadlock the request
    # thread (and, with it, every other thread waiting on the same lock).
    with pending_lock:
        pending_list = load_pending_deletions()

        # Group episodes by series for batched deletion
        episodes_by_series = defaultdict(list)
        for series in pending_list:
            series_title = series['series_title']
            for season_data in list(series['seasons'].values()):
                for episode in list(season_data['episodes']):
                    if episode['episode_id'] in episode_ids:
                        episodes_by_series[series_title].append(episode)

    deleted_count = 0
    errors = []

    # Delete episodes in batches per series (MORE EFFICIENT!)
    for series_title, episodes in episodes_by_series.items():
        try:
            # Build the episode dicts delete_episodes_immediately() expects
            # (id, episodeFileId, seasonNumber, episodeNumber, title), and
            # grab the series_id off any one of them — they're all the same
            # series since we grouped by series_title above.
            series_id = None
            episode_list = []
            for episode in episodes:
                episode_data = episode['episode_data']
                episode_file_id = episode_data.get('episodeFile', {}).get('id')
                if not episode_file_id:
                    errors.append(f"No file ID for episode {episode['episode_id']}")
                    continue
                if series_id is None:
                    series_id = episode_data.get('seriesId')
                episode_list.append({
                    'id': episode_data.get('id'),
                    'episodeFileId': episode_file_id,
                    'seasonNumber': episode_data.get('seasonNumber'),
                    'episodeNumber': episode_data.get('episodeNumber'),
                    'title': episode_data.get('title'),
                })

            if episode_list:
                # ONE DELETE CALL FOR ALL EPISODES IN THIS SERIES.
                # force=True bypasses BOTH global and rule-level dry-run:
                # approving from the pending queue is the explicit human
                # confirmation to delete now. (rule_dry_run=False alone isn't
                # enough — global dry_run_mode defaults to True and would
                # still route this back into the queueing path.)
                logger.info(f"Deleting {len(episode_list)} episodes from {series_title} in batch")
                sonarr_delete_func(episode_list, series_id, series_title,
                                    reason="Approved from pending deletions",
                                    rule_dry_run=False, force=True)
                deleted_count += len(episode_list)

                # Log individual episodes
                for episode in episodes:
                    episode_data = episode['episode_data']
                    logger.info(f"✓ Deleted: {series_title} S{episode_data['seasonNumber']:02d}E{episode_data['episodeNumber']:02d}")

        except Exception as e:
            error_msg = f"Failed to delete episodes from {series_title}: {str(e)}"
            errors.append(error_msg)
            logger.error(error_msg)

    # Remove deleted episodes from pending list
    with pending_lock:
        pending_list = load_pending_deletions()
        for series in pending_list:
            for season_key, season_data in list(series['seasons'].items()):
                season_data['episodes'] = [
                    ep for ep in season_data['episodes']
                    if ep['episode_id'] not in episode_ids
                ]
                # Remove empty seasons
                if not season_data['episodes']:
                    del series['seasons'][season_key]

        # Remove empty series
        pending_list = [s for s in pending_list if s['seasons']]

        save_pending_deletions(pending_list)

    return {
        'deleted_count': deleted_count,
        'errors': errors
    }


def reject_deletions(episode_ids):
    """
    Reject deletions for specified episodes and add to rejection cache
    
    Args:
        episode_ids: List of episode IDs to reject
    
    Returns:
        int: Number of episodes rejected
    """
    with pending_lock:
        pending_list = load_pending_deletions()
        rejected_count = 0
        
        # Add to rejection cache
        with rejection_lock:
            cache = load_rejection_cache()
            expiry_date = (datetime.now() + timedelta(days=REJECTION_CACHE_DAYS)).strftime('%Y-%m-%d')
            
            for episode_id in episode_ids:
                cache[str(episode_id)] = expiry_date
                rejected_count += 1
            
            save_rejection_cache(cache)
        
        # Remove from pending list
        for series in pending_list:
            for season_data in list(series['seasons'].values()):
                season_data['episodes'] = [
                    ep for ep in season_data['episodes'] 
                    if ep['episode_id'] not in episode_ids
                ]
                # Remove empty seasons
                for season_key in list(series['seasons'].keys()):
                    if not series['seasons'][season_key]['episodes']:
                        del series['seasons'][season_key]
        
        # Remove empty series
        pending_list = [s for s in pending_list if s['seasons']]
        
        save_pending_deletions(pending_list)
        logger.info(f"Rejected {rejected_count} episodes - added to {REJECTION_CACHE_DAYS} day rejection cache")
        
        return rejected_count


def clear_all_pending_deletions():
    """Clear all pending deletions"""
    with pending_lock:
        save_pending_deletions([])
        logger.info("Cleared all pending deletions")


def get_episode_ids_for_series(series_id):
    """Get all episode IDs for a series"""
    with pending_lock:
        pending_list = load_pending_deletions()
        series = next((s for s in pending_list if s['series_id'] == series_id), None)
        
        if not series:
            return []
        
        episode_ids = []
        for season_data in series['seasons'].values():
            episode_ids.extend(ep['episode_id'] for ep in season_data['episodes'])
        
        return episode_ids


def get_episode_ids_for_season(series_id, season_num):
    """Get all episode IDs for a season"""
    with pending_lock:
        pending_list = load_pending_deletions()
        series = next((s for s in pending_list if s['series_id'] == series_id), None)
        
        if not series:
            return []
        
        season_key = str(season_num)
        if season_key not in series['seasons']:
            return []
        
        return [ep['episode_id'] for ep in series['seasons'][season_key]['episodes']]


# ============================================================================
# MOVIE PENDING DELETIONS
# ============================================================================

def _load_movie_rejection_cache():
    try:
        if os.path.exists(MOVIE_REJECTION_CACHE_FILE):
            with open(MOVIE_REJECTION_CACHE_FILE, 'r') as f:
                cache = json.load(f)
            today = datetime.now().strftime('%Y-%m-%d')
            return {mid: exp for mid, exp in cache.items() if exp >= today}
        return {}
    except Exception as e:
        logger.error(f"Error loading movie rejection cache: {e}")
        return {}


def _save_movie_rejection_cache(cache):
    try:
        with open(MOVIE_REJECTION_CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving movie rejection cache: {e}")


def is_movie_rejected(movie_id):
    with movie_rejection_lock:
        cache = _load_movie_rejection_cache()
        return str(movie_id) in cache


def queue_movie_deletion(movie_id, movie_title, movie_file_id, file_size,
                          rule_name, reason, date_source, date_value, delete_option='file_only'):
    """Add a movie to the pending deletions queue."""
    if is_movie_rejected(movie_id):
        logger.debug(f"Skipping movie {movie_id} — in rejection cache")
        return

    with pending_lock:
        data = _load_raw()
        movies = data.get("movies", [])

        if any(m['movie_id'] == movie_id for m in movies):
            logger.debug(f"Movie {movie_id} already in pending deletions")
            return

        movies.append({
            'movie_id': movie_id,
            'movie_title': movie_title,
            'movie_file_id': movie_file_id,
            'file_size_mb': round(file_size / (1024 * 1024), 2) if file_size else 0,
            'rule_name': rule_name,
            'reason': reason,
            'date_source': date_source,
            'date_value': date_value,
            'delete_option': delete_option,
            'queued_at': datetime.now().isoformat(),
        })

        data["movies"] = movies
        _save_raw(data)
        logger.info(f"Queued movie for deletion: '{movie_title}' — {reason}")


def load_pending_movies():
    """Return the movies pending-deletion list."""
    return _load_raw().get("movies", [])


def get_pending_movies_summary():
    movies = load_pending_movies()
    total_size = sum(m.get('file_size_mb', 0) for m in movies)
    return {
        'total_movies': len(movies),
        'total_size_mb': total_size,
        'total_size_gb': round(total_size / 1024, 2),
        'movies': movies,
    }


def approve_movie_deletions(movie_ids):
    """Execute approved movie deletions via Radarr."""
    from movie_processor import get_radarr_settings
    from episeerr_utils import http, normalize_url

    radarr_url, api_key = get_radarr_settings()
    if not radarr_url or not api_key:
        return {'deleted_count': 0, 'errors': ['Radarr not configured']}

    headers = {'X-Api-Key': api_key}
    deleted_count = 0
    errors = []

    with pending_lock:
        data = _load_raw()
        movies = data.get("movies", [])

        to_delete = [m for m in movies if m['movie_id'] in movie_ids]
        remaining = [m for m in movies if m['movie_id'] not in movie_ids]

        for movie in to_delete:
            try:
                delete_option = movie.get('delete_option', 'file_only')
                if delete_option == 'remove_from_radarr':
                    url = f"{radarr_url}/api/v3/movie/{movie['movie_id']}?deleteFiles=true"
                    resp = http.delete(url, headers=headers, timeout=15)
                else:
                    url = f"{radarr_url}/api/v3/moviefile/{movie['movie_file_id']}"
                    resp = http.delete(url, headers=headers, timeout=15)

                if resp.ok:
                    deleted_count += 1
                    logger.info(f"✅ Deleted movie '{movie['movie_title']}' ({delete_option})")
                else:
                    errors.append(f"Failed to delete '{movie['movie_title']}': {resp.status_code}")
                    logger.error(f"Failed to delete movie {movie['movie_id']}: {resp.text[:200]}")
            except Exception as e:
                errors.append(f"Error deleting '{movie['movie_title']}': {str(e)}")
                logger.error(f"Error deleting movie {movie['movie_id']}: {e}")

        data["movies"] = remaining
        _save_raw(data)

    return {'deleted_count': deleted_count, 'errors': errors}


def reject_movie_deletions(movie_ids):
    """Reject movie deletions and cache them."""
    with movie_rejection_lock:
        cache = _load_movie_rejection_cache()
        expiry = (datetime.now() + timedelta(days=REJECTION_CACHE_DAYS)).strftime('%Y-%m-%d')
        for mid in movie_ids:
            cache[str(mid)] = expiry
        _save_movie_rejection_cache(cache)

    with pending_lock:
        data = _load_raw()
        data["movies"] = [m for m in data.get("movies", []) if m['movie_id'] not in movie_ids]
        _save_raw(data)

    logger.info(f"Rejected {len(movie_ids)} movie(s) from pending deletions")
    return len(movie_ids)


def clear_all_pending_movies():
    with pending_lock:
        data = _load_raw()
        data["movies"] = []
        _save_raw(data)
        logger.info("Cleared all pending movie deletions")
